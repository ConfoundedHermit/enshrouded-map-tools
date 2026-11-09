// Offline enclosed-air extraction from verified complete solid voxel tiles.
// The column/brick codec follows voxel_codec.py and background_surface.cpp.
// This helper never loads or writes a game image, installation, or KFC file.
//
// --intervals fresh-output.bin input (little endian):
//   10 uint32: 0x31474848, 20480, 256, xmin, xmax, zmin, zmax,
//              ymin, ymax, minimumAirCells.
//   Repeated uint32 tx, tz, byteCount followed by complete finest tile bytes.
//   One final uint32 0xffffffff. Every intersecting tile is required exactly once.
//   Bounds are half-open, measured in 0.5m cells with world origin (0,0,0).
// Output: raw 12-byte records <6H: gx,gz,lo,hi,floorRaw,ceilingRaw.
//   [lo,hi) contains only air; y=lo-1 and y=hi are occupied. Both solids must
//   lie inside the requested vertical bounds. No exterior/below-world air.
// --interval-column input: four uint32 ymin,ymax,minAir,reserved=0, then a
//   uint32 byteCount and complete column bytes. Same output, local x/z 0..7.
// --brick input: uint32 byteCount and compressed bytes; output 512 uint16 cells
//   in native x+8*y+64*z order, matching the earlier surface helper.
// --components intervals.bin fresh-labels.bin: connect neighboring X/Z air
//   intervals when their half-open height intervals overlap by at least one
//   cell. Output uint32 compact component IDs in the original record order.
//   Each horizontal column must have disjoint intervals in ascending order.
// --surface-components intervals.bin fresh-labels.bin: the same adjacency and
//   output contract, additionally requiring floor-height difference <=2 cells
//   and >=3 air cells above the higher floor shared by both intervals. This is
//   a geometric step-connected surface graph, not a gameplay navigation mesh.
#include <algorithm>
#include <array>
#include <cstdint>
#include <cstdio>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <limits>
#include <numeric>
#include <stdexcept>
#include <string>
#include <vector>
#include <fcntl.h>
#include <io.h>

using Bytes = std::vector<uint8_t>;
static void require(bool ok, const char* message) {
    if (!ok) throw std::runtime_error(message);
}
static uint32_t u16(const uint8_t* p) {
    return uint32_t(p[0]) | (uint32_t(p[1]) << 8);
}
static uint32_t u24(const uint8_t* p) {
    return u16(p) | (uint32_t(p[2]) << 16);
}

struct Bits {
    const uint8_t* data;
    size_t size, position = 0;
    uint32_t get(unsigned count = 1) {
        require(count <= 32 && position + count <= size * 8,
                "Truncated voxel bitstream");
        // Read at most five bytes, with no unaligned or out-of-bounds loads.
        uint64_t word = 0;
        size_t first = position / 8, end = (position + count + 7) / 8;
        for (size_t at = first; at < end; ++at)
            word |= uint64_t(data[at]) << (8 * (at - first));
        word >>= position % 8;
        position += count;
        return uint32_t(word & ((uint64_t(1) << count) - 1));
    }
    uint32_t gamma() {
        uint32_t value = 1;
        while (get()) {
            require(value <= 0x7fffffff, "Voxel run overflow");
            value = (value << 1) | get();
        }
        return value;
    }
};

static std::array<uint16_t, 512> decode(const uint8_t* data, size_t size) {
    Bits bits{data, size};
    size_t back = size;
    auto literal = [&]() {
        require(back > 0, "Missing voxel literal");
        return data[--back];
    };
    std::array<uint8_t, 512> materials{};
    size_t at = 0;
    uint8_t previous = 0, last = 0;
    while (at < 512) {
        uint8_t material = bits.get() ? literal() : previous;
        previous = last;
        last = material;
        uint32_t run = bits.gamma();
        require(run <= 512 - at, "Voxel material run too long");
        std::fill_n(materials.begin() + at, run, material);
        at += run;
    }
    std::array<uint16_t, 512> result{};
    at = 0;
    uint8_t lastDensity = 0;
    while (at < 512) {
        uint8_t material = materials[at], density;
        if (bits.get()) {
            density = literal();
            if (material >= 128) lastDensity = density;
        } else {
            density = material == 0 ? 0 : material < 128 ? 255 : lastDensity;
        }
        uint32_t run = material < 128 && density > 0 && density < 255
                           ? 1 : bits.gamma();
        require(run <= 512 - at, "Voxel density run too long");
        for (size_t i = at; i < at + run; ++i)
            result[i] = uint16_t(materials[i]) | (uint16_t(density) << 8);
        at += run;
    }
    require((bits.position + 7) / 8 == back,
            "Brick bitstream and literal tail do not meet");
    return result;
}

static bool solid(uint16_t raw) {
    unsigned material = raw & 255, density = raw >> 8;
    return material && (material < 128 ? density >= 128 : density != 0);
}

struct Interval {
    uint16_t x, z, lo, hi, floorRaw, ceilingRaw;
};
static_assert(sizeof(Interval) == 12, "Unexpected interval padding");

struct Bounds {
    uint32_t xmin, xmax, zmin, zmax, ymin, ymax, minAir;
    void validate() const {
        require(xmin < xmax && xmax <= 20480 && zmin < zmax && zmax <= 20480,
                "Invalid horizontal cell bounds");
        require(ymin < ymax && ymax <= 65535 && minAir && minAir < ymax - ymin,
                "Invalid vertical cell bounds or minimum air interval");
    }
};

struct ColumnIntervals {
    const Bounds& bounds;
    uint32_t baseX, baseZ;
    std::vector<Interval>& output;
    std::array<bool, 64> occupied{};
    std::array<uint16_t, 64> lastRaw{}, floorRaw{};
    std::array<uint32_t, 64> start{};
    std::array<bool, 64> active{};

    ColumnIntervals(const Bounds& b, uint32_t x, uint32_t z,
                    std::vector<Interval>& target)
        : bounds(b), baseX(x), baseZ(z), output(target) {
        start.fill(std::numeric_limits<uint32_t>::max());
        for (unsigned z = 0; z < 8; ++z)
            for (unsigned x = 0; x < 8; ++x)
                active[x + 8*z] = baseX + x >= b.xmin && baseX + x < b.xmax
                                 && baseZ + z >= b.zmin && baseZ + z < b.zmax;
    }

    void cell(unsigned at, uint32_t y, uint16_t raw) {
        if (!active[at]) return;
        if (solid(raw)) {
            if (!occupied[at]
                && start[at] != std::numeric_limits<uint32_t>::max()
                && y - start[at] >= bounds.minAir) {
                output.push_back({uint16_t(baseX + at % 8),
                                  uint16_t(baseZ + at / 8),
                                  uint16_t(start[at]), uint16_t(y),
                                  floorRaw[at], raw});
            }
            occupied[at] = true;
            lastRaw[at] = raw;
            start[at] = std::numeric_limits<uint32_t>::max();
        } else {
            if (occupied[at]) {
                start[at] = y;
                floorRaw[at] = lastRaw[at];
            }
            occupied[at] = false;
        }
    }

    void constant(uint32_t lo, uint32_t hi, uint16_t raw) {
        lo = std::max(lo, bounds.ymin);
        hi = std::min(hi, bounds.ymax);
        if (lo >= hi) return;
        // A constant run changes state only at its first cell. Its final raw
        // value is the same, so this handles arbitrarily tall runs in O(64).
        for (unsigned at = 0; at < 64; ++at) cell(at, lo, raw);
    }

    void brick(uint32_t by, const uint8_t* data, size_t size) {
        uint32_t lo = std::max(by * 8, bounds.ymin);
        uint32_t hi = std::min(by * 8 + 8, bounds.ymax);
        if (lo >= hi) return;
        auto values = decode(data, size);
        for (uint32_t y = lo; y < hi; ++y)
            for (unsigned z = 0; z < 8; ++z)
                for (unsigned x = 0; x < 8; ++x)
                    cell(x + 8*z, y, values[x + 8*(y % 8) + 64*z]);
    }
};

static void intervals(const uint8_t* data, size_t size, const Bounds& bounds,
                      uint32_t x, uint32_t z, std::vector<Interval>& output) {
    if (!size) return;
    require(size >= 4, "Truncated column header");
    uint32_t height = u16(data), metadataSize = u16(data + 2);
    require(metadataSize <= size - 4, "Column metadata exceeds payload");
    Bits bits{data + 4, metadataSize};
    const uint8_t* payload = data + 4 + metadataSize;
    size_t payloadSize = size - 4 - metadataSize, offset = 0;
    uint8_t material = 0, density = 0;
    uint32_t y = 0;
    ColumnIntervals column(bounds, x, z, output);
    while (y < height) {
        uint32_t run = bits.gamma(), compressed = bits.get();
        require(run <= height - y, "Column run exceeds declared height");
        if (!compressed) {
            if (bits.get()) {
                require(offset < payloadSize, "Missing column material");
                material = payload[offset++];
                if (bits.get()) {
                    require(offset < payloadSize, "Missing column density");
                    density = payload[offset++];
                } else density = material ? 255 : 0;
            }
            uint16_t raw = uint16_t(material) | (uint16_t(density) << 8);
            column.constant(y * 8, (y + run) * 8, raw);
        } else {
            for (uint32_t j = 0; j < run; ++j) {
                uint32_t n = bits.get(10);
                require(n && n <= payloadSize - offset,
                        "Compressed brick exceeds column payload");
                column.brick(y + j, payload + offset, n);
                offset += n;
            }
        }
        y += run;
    }
    require(offset == payloadSize && (bits.position + 7) / 8 == metadataSize,
            "Unconsumed column payload or metadata");
    // The absent volume above a column is air. An interval without an encoded
    // solid ceiling is deliberately not emitted.
}

static void input(void* target, size_t size) {
    require(bool(std::cin.read(static_cast<char*>(target), size)),
            "Truncated input frame");
}
static uint32_t input32() {
    uint32_t value;
    input(&value, sizeof value);
    return value;
}
static Bytes frame() {
    uint32_t size = input32();
    require(size <= 16*1024*1024, "Oversized tile or column input frame");
    Bytes result(size);
    if (size) input(result.data(), size);
    return result;
}
static void finishInput() {
    require(std::cin.peek() == std::char_traits<char>::eof(),
            "Extra input bytes after complete input");
}
static void writeIntervals(std::ostream& output,
                           const std::vector<Interval>& records) {
    if (!records.empty())
        output.write(reinterpret_cast<const char*>(records.data()),
                     std::streamsize(records.size() * sizeof(Interval)));
    require(bool(output), "Interval output write failed");
}

static uint32_t findRoot(std::vector<uint32_t>& parents, uint32_t at) {
    uint32_t root = at;
    while (parents[root] != root) root = parents[root];
    while (parents[at] != at) {
        uint32_t next = parents[at];
        parents[at] = root;
        at = next;
    }
    return root;
}

static void components(const char* inputPath, const char* outputPath,
                       bool surfaces = false) {
    require(!std::filesystem::exists(outputPath),
            "Expected fresh component-label destination");
    uint64_t bytes = std::filesystem::file_size(inputPath);
    require(bytes && bytes % sizeof(Interval) == 0,
            "Invalid or empty enclosed-interval file");
    uint64_t count64 = bytes / sizeof(Interval);
    require(count64 < 0x80000000, "Too many intervals for component indexing");
    uint32_t count = uint32_t(count64);
    std::vector<Interval> records(count);
    std::ifstream inputFile(inputPath, std::ios::binary);
    inputFile.read(reinterpret_cast<char*>(records.data()), std::streamsize(bytes));
    require(bool(inputFile), "Cannot read complete interval file");
    uint32_t xmin = 20480, xmax = 0, zmin = 20480, zmax = 0;
    for (const auto& r : records) {
        require(r.x < 20480 && r.z < 20480 && r.lo < r.hi
                && solid(r.floorRaw) && solid(r.ceilingRaw),
                "Invalid enclosed-interval record");
        xmin = std::min(xmin, uint32_t(r.x));
        xmax = std::max(xmax, uint32_t(r.x));
        zmin = std::min(zmin, uint32_t(r.z));
        zmax = std::max(zmax, uint32_t(r.z));
    }
    uint32_t width = xmax-xmin+1, depth = zmax-zmin+1;
    constexpr uint32_t none = 0xffffffff;
    std::vector<uint32_t> head(size_t(width)*depth, none), next(count, none);
    std::vector<uint32_t> parents(count);
    std::iota(parents.begin(), parents.end(), 0u);
    std::vector<uint8_t> ranks(count, 0);
    // Reverse insertion preserves the original ascending order in each cell.
    for (uint32_t i = count; i-- > 0;) {
        const auto& r = records[i];
        size_t cell = size_t(r.z-zmin)*width + (r.x-xmin);
        if (head[cell] != none)
            require(r.hi <= records[head[cell]].lo,
                    "Per-column intervals are not ascending and disjoint");
        next[i] = head[cell];
        head[cell] = i;
    }
    uint64_t sharedEdges = 0, joins = 0;
    auto connect = [&](uint32_t a, uint32_t b) {
        while (a != none && b != none) {
            const auto& ar = records[a];
            const auto& br = records[b];
            uint32_t sharedLo = std::max(ar.lo, br.lo);
            uint32_t sharedHi = std::min(ar.hi, br.hi);
            uint32_t floorDifference = std::max(ar.lo, br.lo)
                                       - std::min(ar.lo, br.lo);
            bool adjacent = surfaces
                ? floorDifference <= 2 && sharedHi >= sharedLo + 3
                : sharedLo < sharedHi;
            if (adjacent) {
                ++sharedEdges;
                uint32_t ra = findRoot(parents, a), rb = findRoot(parents, b);
                if (ra != rb) {
                    if (ranks[ra] < ranks[rb]) std::swap(ra, rb);
                    parents[rb] = ra;
                    if (ranks[ra] == ranks[rb]) ++ranks[ra];
                    ++joins;
                }
            }
            if (ar.hi <= br.hi) a = next[a];
            else b = next[b];
        }
    };
    for (uint32_t z = 0; z < depth; ++z) {
        for (uint32_t x = 0; x < width; ++x) {
            size_t cell = size_t(z)*width+x;
            if (head[cell] == none) continue;
            if (x+1 < width) connect(head[cell], head[cell+1]);
            if (z+1 < depth) connect(head[cell], head[cell+width]);
        }
        if (z % 256 == 255)
            std::cerr << "Connected interval row " << z+1 << '/' << depth << '\n';
    }
    // Flatten before reusing the parent array for compact labels. With fewer
    // than 2^31 records the high bit safely distinguishes assigned labels.
    for (uint32_t i = 0; i < count; ++i) parents[i] = findRoot(parents, i);
    constexpr uint32_t assigned = 0x80000000;
    std::vector<uint32_t> sizes;
    for (uint32_t i = 0; i < count; ++i) {
        uint32_t root = parents[i], label;
        if (root & assigned) label = root & ~assigned;
        else {
            if (!(parents[root] & assigned)) {
                parents[root] = assigned | uint32_t(sizes.size());
                sizes.push_back(0);
            }
            label = parents[root] & ~assigned;
        }
        ++sizes[label];
        parents[i] = assigned | label;
    }
    for (auto& label : parents) label &= ~assigned;
    std::ofstream output(outputPath, std::ios::binary);
    output.write(reinterpret_cast<const char*>(parents.data()),
                 std::streamsize(parents.size()*sizeof(uint32_t)));
    output.close();
    require(bool(output), "Component label output failed");
    std::vector<uint32_t> order(sizes.size());
    std::iota(order.begin(), order.end(), 0u);
    std::sort(order.begin(), order.end(), [&](uint32_t a, uint32_t b) {
        return sizes[a] > sizes[b] || (sizes[a] == sizes[b] && a < b);
    });
    std::cerr << "Components " << sizes.size() << ", records " << count
              << (surfaces ? ", shared surface edges " : ", shared air edges ")
              << sharedEdges << ", unions " << joins
              << ", cell bounds [" << xmin << ',' << xmax+1 << ")x["
              << zmin << ',' << zmax+1 << ")\n";
    for (size_t i = 0; i < std::min(size_t(20), order.size()); ++i)
        std::cerr << "Component " << order[i] << ": " << sizes[order[i]]
                  << " intervals\n";
}

int main(int argc, char** argv) {
    try {
        _setmode(_fileno(stdin), _O_BINARY);
        _setmode(_fileno(stdout), _O_BINARY);
        require(argc >= 2, "Use --brick, --interval-column, or --intervals output.bin");
        std::string mode = argv[1];
        if (mode == "--components") {
            require(argc == 4, "Use --components intervals.bin fresh-labels.bin");
            components(argv[2], argv[3]);
            return 0;
        }
        if (mode == "--surface-components") {
            require(argc == 4,
                    "Use --surface-components intervals.bin fresh-labels.bin");
            components(argv[2], argv[3], true);
            return 0;
        }
        if (mode == "--brick") {
            require(argc == 2, "Unexpected brick arguments");
            auto data = frame();
            auto values = decode(data.data(), data.size());
            finishInput();
            std::cout.write(reinterpret_cast<const char*>(values.data()), 1024);
            require(bool(std::cout), "Brick output write failed");
            return 0;
        }
        if (mode == "--interval-column") {
            require(argc == 2, "Unexpected column arguments");
            std::array<uint32_t, 4> h{};
            input(h.data(), sizeof h);
            require(h[3] == 0, "Nonzero reserved column header");
            Bounds b{0, 8, 0, 8, h[0], h[1], h[2]};
            b.validate();
            auto data = frame();
            finishInput();
            std::vector<Interval> records;
            intervals(data.data(), data.size(), b, 0, 0, records);
            writeIntervals(std::cout, records);
            return 0;
        }
        require(mode == "--intervals" && argc == 3
                && !std::filesystem::exists(argv[2]),
                "Expected fresh --intervals output.bin destination");
        std::array<uint32_t, 10> h{};
        input(h.data(), sizeof h);
        require(h[0] == 0x31474848 && h[1] == 20480 && h[2] == 256,
                "Wrong finest-grid interval header");
        Bounds b{h[3],h[4],h[5],h[6],h[7],h[8],h[9]};
        b.validate();
        std::array<bool, 6400> seen{};
        uint32_t expected = ((b.xmax - 1)/256 - b.xmin/256 + 1)
                            * ((b.zmax - 1)/256 - b.zmin/256 + 1);
        std::ofstream output(argv[2], std::ios::binary);
        require(bool(output), "Cannot create interval output");
        uint64_t recordCount = 0;
        uint32_t tiles = 0;
        for (;;) {
            uint32_t tx = input32();
            if (tx == 0xffffffff) break;
            uint32_t tz = input32();
            require(tx < 80 && tz < 80 && tx >= b.xmin/256
                    && tx <= (b.xmax-1)/256 && tz >= b.zmin/256
                    && tz <= (b.zmax-1)/256, "Unexpected tile coordinates");
            require(!seen[tx + 80*tz], "Duplicate tile coordinates");
            seen[tx + 80*tz] = true;
            auto data = frame();
            require(data.size() >= 2, "Truncated complete tile");
            unsigned width = data[0], depth = data[1], count = width * depth;
            size_t base = 2 + 3*count;
            require(width == 32 && depth == 32 && base <= data.size(),
                    "Wrong finest tile dimensions");
            std::vector<Interval> records;
            records.reserve(262144);
            for (unsigned i = 0; i < count; ++i) {
                size_t start = u24(data.data() + 2 + 3*i);
                size_t end = i+1 == count ? data.size()-base
                             : u24(data.data() + 5 + 3*i);
                require(start <= end && end <= data.size()-base
                        && (i || start == 0), "Invalid complete tile offsets");
                uint32_t x = tx*256 + (i % width)*8;
                uint32_t z = tz*256 + (i / width)*8;
                if (x >= b.xmax || x+8 <= b.xmin
                    || z >= b.zmax || z+8 <= b.zmin) continue;
                intervals(data.data() + base + start, end-start, b, x, z, records);
            }
            writeIntervals(output, records);
            recordCount += records.size();
            ++tiles;
            std::cerr << "Decoded tile " << tx << ',' << tz << " (" << tiles
                      << '/' << expected << "), intervals " << recordCount << '\n';
        }
        finishInput();
        require(tiles == expected, "Missing complete tiles in requested region");
        output.close();
        require(bool(output), "Interval output close failed");
        std::cerr << "Complete: " << tiles << " tiles, " << recordCount
                  << " enclosed intervals, " << recordCount*12 << " bytes\n";
        return 0;
    } catch (const std::exception& e) {
        std::cerr << e.what() << '\n';
        return 1;
    }
}
