"""Render one north-up map per Hollow Hall from all selected floor surfaces.

The native half-metre floor samples retain their X/Z footprint. The highest
surface supplies the base relief; lower surfaces blend into the same pixels,
with restrained edge accents. This is a flattened projection, so overlapping
levels cannot be read as separate routes or gameplay storeys.
"""
from __future__ import annotations

import hashlib
import math
from pathlib import Path
import struct

from .delivery import png_from_rgb
from .delivery import verify_png
from .hall_render import (
    PALETTE, _Canvas, _bounds, _draw_marker, _draw_surface, _mix,
    _projection, _text_width, _wrap,
)


def flatten_surfaces(samples, bounds):
    """Group every in-bounds surface in deterministic upper-to-lower order.

    Returned layers use native local pixel coordinates, one pixel per cell.
    Their union is the original X/Z projection, and every supplied sample is
    accounted for as either an in-bounds contributor or an excluded sample.
    """
    bx, bz, ex, ez = _bounds(bounds)
    columns = {}
    excluded = 0
    for sample in samples:
        if len(sample) != 4:
            raise ValueError('A sample must contain x, z, floor and ceiling cells')
        gx, gz, floor, ceiling = sample
        if (type(gx) is not int or type(gz) is not int
                or not all(isinstance(v, (int, float)) and math.isfinite(v)
                           for v in (floor, ceiling)) or ceiling <= floor):
            raise ValueError('Invalid floor sample or non-positive ceiling clearance')
        if not (bx <= gx < ex and bz <= gz < ez):
            excluded += 1
            continue
        columns.setdefault((gx, gz), []).append((floor, ceiling))
    if not columns:
        raise ValueError('A flattened map requires at least one in-bounds surface')
    layers = []
    sample_hash = hashlib.sha256()
    union_hash = hashlib.sha256()
    for (gx, gz), values in sorted(columns.items()):
        values.sort(reverse=True)
        union_hash.update(struct.pack('<qq', gx, gz))
        position = (gx - bx, ez - gz - 1)
        for rank, (floor, ceiling) in enumerate(values):
            if rank == len(layers):
                layers.append({})
            layers[rank][position] = (floor, ceiling - floor)
            sample_hash.update(struct.pack('<qqdd', gx, gz, floor, ceiling))
    source_count = sum(map(len, layers))
    receipt = {
        'method': 'Upper-surface relief with lower-surface colour and edge compositing at identical X/Z.',
        'source_samples': source_count,
        'contributing_samples': source_count,
        'upper_surface_samples': len(columns),
        'lower_surface_samples': source_count - len(columns),
        'unique_xz_columns': len(columns),
        'stacked_columns': sum(len(values) > 1 for values in columns.values()),
        'stacked_column_samples': source_count - len(columns),
        'max_surfaces_per_column': len(layers),
        'source_samples_per_depth': list(map(len, layers)),
        'excluded_samples': excluded,
        'projection_union_sha256': union_hash.hexdigest(),
        'ordered_surface_samples_sha256': sample_hash.hexdigest(),
        'hash_encoding': 'Sorted global (x,z); descending (floor,ceiling). Union <qq, surfaces <qqdd little endian.',
    }
    return layers, receipt


def _lower_colour(surface, position, range_y, cell_size):
    """Use the existing green elevation palette and emphasize lower edges."""
    x, y = position
    floor, _ = surface[position]
    lo, hi = range_y
    t = max(0, min(1, (floor - lo) / max(1, hi - lo)))
    colour = _mix(PALETTE['floor_low'], PALETTE['floor_high'], t)
    neighbours = [surface.get((x + dx, y + dy))
                  for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1))]
    edge = any(neighbour is None for neighbour in neighbours)
    if edge:
        colour = _mix(colour, PALETTE['edge'], .62)
    else:
        relief = max(-8, min(8, (neighbours[0][0] - neighbours[2][0]) * cell_size))
        colour = tuple(max(0, min(255, round(channel + relief))) for channel in colour)
        level = math.floor(floor * cell_size / 5)
        if any(math.floor(n[0] * cell_size / 5) != level for n in neighbours[:3:2]):
            colour = _mix(colour, PALETTE['edge'], .2)
    return colour, edge


def _draw_flat_surface(canvas, origin, layers, range_y, cell_size):
    """Blend each selected lower sample once, without moving or filling it."""
    _draw_surface(canvas, origin, layers[0], range_y, cell_size)
    ox, oy = origin
    changed = set()
    lower_edges = 0
    for rank, layer in enumerate(layers[1:], 1):
        for position in layer:
            colour, edge = _lower_colour(layer, position, range_y, cell_size)
            lower_edges += edge
            # Lower fragments remain legible while the upper floor retains its
            # visual weight. Deeper overlaps have progressively softer accents.
            opacity = (.36 if edge else .22) * .75 ** (rank - 1)
            x, y = ox + position[0], oy + position[1]
            offset = (y * canvas.width + x) * 3
            previous = tuple(canvas.rgb[offset:offset + 3])
            blended = _mix(previous, colour, opacity)
            canvas.rgb[offset:offset + 3] = bytes(blended)
            if previous != blended:
                changed.add(position)
    return {'lower_surface_edge_samples': lower_edges,
            'lower_composite_changed_pixels': len(changed),
            'lower_edge_opacity': .36, 'lower_fill_opacity': .22,
            'deeper_layer_opacity_multiplier': .75}


def render_flat_map(path, hallname, bounds, samples, markers=(), *,
                    build='26430625', cell_size=.5, pixels_per_metre=2.0,
                    note='Static geometry | overlapping surfaces combined'):
    """Write a fresh single-panel PNG and return its complete geometry receipt.

    Scene markers use continuous cell coordinates, independent of their former
    elevation-panel assignment. The retained native scale is one pixel per
    half-metre sample, or two pixels per metre; no footprint is downsampled.
    """
    path = Path(path)
    if path.exists():
        raise ValueError('Use a fresh flattened-map filename')
    bounds = _bounds(bounds)
    if cell_size != .5 or pixels_per_metre != 2.0:
        raise ValueError('Flattened maps retain the native half-metre scale at two pixels per metre')
    layers, composition = flatten_surfaces(samples, bounds)
    range_y = [min(row[0] for layer in layers for row in layer.values()),
               max(row[0] for layer in layers for row in layer.values()) + 1]
    markers = list(markers)
    ppc = cell_size * pixels_per_metre
    w, h, position = _projection(bounds, ppc)
    pad = 24
    panel_width = max(440, w + pad * 2)
    panel_height = h + 145
    width = max(960, pad * 2 + panel_width)
    title_lines = _wrap(hallname, width - 2 * pad - 110, 3)
    header_height = 34 + len(title_lines) * 29 + 44
    footer_lines = _wrap(note, width - pad * 2, 2)
    footer_height = 91 + len(footer_lines) * 21
    height = header_height + panel_height + footer_height
    if max(width, height) > 16000 or width * height > 80_000_000:
        raise ValueError('Flattened map exceeds safe raster dimensions')
    canvas = _Canvas(width, height, PALETTE['paper'])
    for i, line in enumerate(title_lines):
        canvas.text(pad, 26 + i * 29, line, scale=3)
    canvas.text(pad, 34 + len(title_lines) * 29,
                'HOLLOW HALLS / ALL SURFACES COMBINED', PALETTE['muted'], 2)
    canvas.line(pad, header_height - 14, width - pad, header_height - 14, PALETTE['border'])
    cx, cy = width - 65, 43
    canvas.text(cx - 5, cy - 21, 'N', scale=2)
    canvas.line(cx, cy + 23, cx, cy, PALETTE['ink'], 2)
    canvas.line(cx, cy, cx - 7, cy + 13, PALETTE['ink'], 2)
    canvas.line(cx, cy, cx + 7, cy + 13, PALETTE['ink'], 2)
    left, top = pad, header_height
    canvas.rect(left, top, panel_width, panel_height, PALETTE['panel'])
    canvas.rect(left, top, panel_width, 3, PALETTE['border'])
    canvas.text(left + pad, top + 18, 'FLATTENED MAP', scale=3)
    canvas.text(left + pad, top + 75,
                f'Y {range_y[0] * cell_size:g}-{range_y[1] * cell_size:g} M',
                PALETTE['muted'], 1)
    ox, oy = left + (panel_width - w) // 2, top + 98
    bx, bz, ex, ez = bounds
    grid_step = round(25 / cell_size)
    for gx in range(math.ceil(bx / grid_step) * grid_step, ex, grid_step):
        x = ox + gx - bx
        canvas.line(x, oy, x, oy + h - 1, PALETTE['grid'])
    for gz in range(math.ceil(bz / grid_step) * grid_step, ez, grid_step):
        y = oy + ez - gz
        canvas.line(ox, y, ox + w - 1, y, PALETTE['grid'])
    composition.update(_draw_flat_surface(canvas, (ox, oy), layers, range_y, cell_size))
    placed = []
    for source_marker_index, marker in enumerate(markers):
        gx, gz = marker['gx'], marker['gz']
        if not (bx <= gx < ex and bz <= gz < ez):
            continue
        # Sample indices are centered; continuous scene coordinates are not.
        px, py = position(gx - .5, gz - .5)
        x, y = ox + math.floor(px), oy + math.floor(py)
        kind = marker.get('kind', 'checkpoint')
        label = '' if kind == 'portal' else marker.get('label', '')
        label_box = _draw_marker(canvas, x, y, kind, label,
                                 (left, top + 52, left + panel_width, top + panel_height - 28))
        placed.append({'kind': kind, 'label': label, 'world_cells': [gx, gz],
                       'y_cells': marker.get('y_cells'), 'source_marker_index': source_marker_index,
                       'pixel': [x, y], 'label_box_pixels': label_box})
    coincident = {}
    for index, marker in enumerate(placed):
        coincident.setdefault(tuple(marker['world_cells']), []).append(index)
    composition['coincident_marker_positions'] = [
        {'world_cells': list(position), 'marker_indices': indices,
         'count': len(indices), 'kinds': [placed[index]['kind'] for index in indices]}
        for position, indices in sorted(coincident.items()) if len(indices) > 1]
    canvas.text(left + pad, top + panel_height - 27,
                f'{(ex - bx) * cell_size:g} X {(ez - bz) * cell_size:g} M',
                PALETTE['muted'], 2)
    footer_y = header_height + panel_height + 22
    length = 50
    bar_width = round(length * pixels_per_metre)
    for segment in range(4):
        x0 = pad + round(segment * bar_width / 4)
        x1 = pad + round((segment + 1) * bar_width / 4)
        canvas.rect(x0, footer_y, x1 - x0, 7,
                    PALETTE['ink'] if segment % 2 == 0 else PALETTE['border'])
    canvas.text(pad + bar_width + 12, footer_y - 3, f'{length} M', PALETTE['muted'], 2)
    legend_x = pad + bar_width + 110
    for key, label in (('entrance', 'ENTRANCE'), ('exit', 'EXIT'),
                       ('checkpoint', 'CHECKPOINT'), ('portal', 'PORTAL')):
        if any(marker.get('kind', 'checkpoint') == key for marker in markers):
            canvas.circle(legend_x + 5, footer_y + 3, 5, PALETTE[key])
            canvas.text(legend_x + 17, footer_y - 3, label, PALETTE['muted'], 2)
            legend_x += _text_width(label, 2) + 43
    for i, line in enumerate(footer_lines):
        canvas.text(pad, footer_y + 28 + i * 21, line, PALETTE['muted'], 2)
    canvas.text(pad, height - 24,
                f'ENSHROUDED | BUILD {build} | NORTH = +Z / EAST = +X', PALETTE['muted'], 1)
    digest = hashlib.sha256(canvas.rgb).hexdigest()
    path.parent.mkdir(parents=True, exist_ok=True)
    png_from_rgb(path, width, height,
                 (memoryview(canvas.rgb)[y * width * 3:(y + 1) * width * 3]
                  for y in range(height)))
    check = verify_png(path, digest)
    panel = {'index': 0, 'label': 'FLATTENED MAP', 'range_y_cells': range_y,
             'bounds_cells': bounds, 'map_origin_pixels': [ox, oy],
             'map_dimensions_pixels': [w, h], 'pixels_per_metre': pixels_per_metre,
             'source_samples': composition['source_samples'],
             'raster_surface_pixels': composition['unique_xz_columns'],
             'stacked_column_samples': composition['stacked_column_samples'],
             'markers': placed}
    return {'path': str(path), 'dimensions': [width, height], 'rgb_sha256': digest,
            'png_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            'png_verification': check, 'cell_size_metres': cell_size,
            'pixels_per_metre': pixels_per_metre, 'columns': 1, 'panels': [panel],
            'scale_bar_metres': length, 'palette': {key: list(value) for key, value in PALETTE.items()},
            'orientation': 'North is +Z; east is +X. Row zero is north.',
            'representation': 'Geometry-derived floor surfaces composited into one X/Z projection.',
            'flat_composition': composition}
