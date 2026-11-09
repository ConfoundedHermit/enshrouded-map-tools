"""Compose geometry-derived Hollow Halls floor plans as standalone PNG atlases.

Coordinates are integer half-metre cells on the game's X/Z plane. A sample is
``(global_x, global_z, floor_y, ceiling_y)``; ``floor_y`` is the first air cell
above the supporting solid, and ``ceiling_y`` is the exclusive air-column end.
Panels have ``range_y=[minimum, exclusive_maximum]``, ``samples`` and optionally
``label`` and cropped ``bounds=[min_x,min_z,max_x,max_z]``. The renderer makes no
claims about traversability, doors or connections between elevation panels.

Only the Python standard library and the project's existing lossless PNG codec
are used. Its small embedded font makes the export portable and reproducible.
"""
from __future__ import annotations

import hashlib
import math
from pathlib import Path

from .delivery import png_from_rgb
from .delivery import verify_png


PALETTE = {
    'paper': (242, 238, 223),
    'panel': (249, 247, 237),
    'ink': (44, 64, 60),
    'muted': (102, 115, 104),
    'border': (191, 196, 176),
    'grid': (233, 234, 218),
    'floor_low': (123, 154, 135),
    'floor_high': (189, 206, 165),
    'edge': (67, 91, 80),
    'entrance': (169, 68, 44),
    'exit': (47, 101, 130),
    'checkpoint': (138, 105, 41),
    'portal': (92, 75, 139),
}


# Original 5 by 7 pixel lettering, rows encoded as five-bit integers.
_FONT = {
    'A': (14,17,17,31,17,17,17), 'B': (30,17,17,30,17,17,30),
    'C': (14,17,16,16,16,17,14), 'D': (30,17,17,17,17,17,30),
    'E': (31,16,16,30,16,16,31), 'F': (31,16,16,30,16,16,16),
    'G': (14,17,16,23,17,17,15), 'H': (17,17,17,31,17,17,17),
    'I': (14,4,4,4,4,4,14), 'J': (7,2,2,2,18,18,12),
    'K': (17,18,20,24,20,18,17), 'L': (16,16,16,16,16,16,31),
    'M': (17,27,21,21,17,17,17), 'N': (17,25,21,19,17,17,17),
    'O': (14,17,17,17,17,17,14), 'P': (30,17,17,30,16,16,16),
    'Q': (14,17,17,17,21,18,13), 'R': (30,17,17,30,20,18,17),
    'S': (15,16,16,14,1,1,30), 'T': (31,4,4,4,4,4,4),
    'U': (17,17,17,17,17,17,14), 'V': (17,17,17,17,17,10,4),
    'W': (17,17,17,21,21,21,10), 'X': (17,17,10,4,10,17,17),
    'Y': (17,17,10,4,4,4,4), 'Z': (31,1,2,4,8,16,31),
    '0': (14,17,19,21,25,17,14), '1': (4,12,4,4,4,4,14),
    '2': (14,17,1,2,4,8,31), '3': (30,1,1,14,1,1,30),
    '4': (2,6,10,18,31,2,2), '5': (31,16,16,30,1,1,30),
    '6': (14,16,16,30,17,17,14), '7': (31,1,2,4,8,8,8),
    '8': (14,17,17,14,17,17,14), '9': (14,17,17,15,1,1,14),
    ' ': (0,0,0,0,0,0,0), '-': (0,0,0,31,0,0,0),
    '.': (0,0,0,0,0,12,12), ',': (0,0,0,0,0,4,8),
    ':': (0,12,12,0,12,12,0), '/': (1,2,2,4,8,8,16),
    '|': (4,4,4,4,4,4,4), '+': (0,4,4,31,4,4,0),
    '(': (2,4,8,8,8,4,2), ')': (8,4,2,2,2,4,8),
    '[': (14,8,8,8,8,8,14), ']': (14,2,2,2,2,2,14),
    "'": (4,4,8,0,0,0,0), '?': (14,17,1,2,4,0,4),
    '=': (0,0,31,0,31,0,0), '%': (17,2,4,8,16,17,0),
}


class _Canvas:
    def __init__(self, width, height, color):
        self.width, self.height = width, height
        self.rgb = bytearray(bytes(color) * (width * height))

    def rect(self, x, y, width, height, color):
        x0, y0 = max(0, int(x)), max(0, int(y))
        x1, y1 = min(self.width, int(x + width)), min(self.height, int(y + height))
        if x1 <= x0 or y1 <= y0:
            return
        row = bytes(color) * (x1 - x0)
        for py in range(y0, y1):
            offset = (py * self.width + x0) * 3
            self.rgb[offset:offset + len(row)] = row

    def line(self, x0, y0, x1, y1, color, thickness=1):
        x0,y0,x1,y1 = map(round, (x0,y0,x1,y1))
        dx,dy = abs(x1-x0), -abs(y1-y0)
        sx,sy = (1 if x0<x1 else -1), (1 if y0<y1 else -1)
        error = dx+dy
        while True:
            self.rect(x0-thickness//2,y0-thickness//2,thickness,thickness,color)
            if x0==x1 and y0==y1:
                return
            twice = error*2
            if twice>=dy:
                error+=dy
                x0+=sx
            if twice<=dx:
                error+=dx
                y0+=sy

    def text(self, x, y, text, color=None, scale=2):
        color = color or PALETTE['ink']
        for i, letter in enumerate(str(text).upper()):
            for row, bits in enumerate(_FONT.get(letter, _FONT['?'])):
                for column in range(5):
                    if bits & (1 << (4-column)):
                        self.rect(x+(i*6+column)*scale,y+row*scale,scale,scale,color)

    def circle(self, x, y, radius, color):
        for dy in range(-radius,radius+1):
            width = math.floor(math.sqrt(radius*radius-dy*dy))
            self.rect(x-width,y+dy,width*2+1,1,color)


def _text_width(text, scale=2):
    return max(0, len(str(text))*6-1)*scale


def _wrap(text, width, scale=2):
    maximum = max(1,(width//scale+1)//6)
    lines, current = [], ''
    for word in str(text).split():
        candidate = current+' '+word if current else word
        if len(candidate)>maximum and current:
            lines.append(current)
            current=word
        else:
            current=candidate
    if current:
        lines.append(current)
    return lines


def _bounds(bounds):
    if len(bounds)!=4 or any(type(n) is not int for n in bounds):
        raise ValueError('Bounds must be four integer global cell coordinates')
    if bounds[2]<=bounds[0] or bounds[3]<=bounds[1]:
        raise ValueError('Bounds must have positive width and height')
    return list(bounds)


def _projection(bounds, pixels_per_cell):
    """Return sample-centre projection with north (+Z) at the top."""
    bx,bz,ex,ez=bounds
    width=math.ceil((ex-bx)*pixels_per_cell)
    height=math.ceil((ez-bz)*pixels_per_cell)
    def position(gx,gz):
        return ((gx+.5-bx)*pixels_per_cell,
                (ez-gz-.5)*pixels_per_cell)
    return width,height,position


def _surface(samples, bounds, pixels_per_cell):
    """Rasterize native cell extents without filling unsampled gaps.

    Downsampling averages elevations and conservatively retains the lowest
    clearance. More than one vertical surface per native column is reported;
    the highest surface is visible in the two-dimensional panel.
    """
    bx,bz,ex,ez=bounds
    columns={}
    count=0
    stacked=0
    for sample in samples:
        if len(sample)!=4:
            raise ValueError('A sample must contain x, z, floor and ceiling cells')
        gx,gz,floor,ceiling=sample
        if (type(gx) is not int or type(gz) is not int
                or not all(isinstance(v,(int,float)) and math.isfinite(v) for v in (floor,ceiling))
                or ceiling<=floor):
            raise ValueError('Invalid floor sample or non-positive ceiling clearance')
        if not (bx<=gx<ex and bz<=gz<ez):
            continue
        count+=1
        key=(gx,gz)
        old=columns.get(key)
        if old is not None:
            stacked+=1
        if old is None or floor>old[0]:
            columns[key]=(floor,ceiling)
    pixels={}
    for (gx,gz),(floor,ceiling) in columns.items():
        px0=math.floor((gx-bx)*pixels_per_cell)
        px1=math.ceil((gx+1-bx)*pixels_per_cell)
        py0=math.floor((ez-gz-1)*pixels_per_cell)
        py1=math.ceil((ez-gz)*pixels_per_cell)
        for py in range(py0,py1):
            for px in range(px0,px1):
                previous=pixels.get((px,py))
                if previous is None:
                    pixels[(px,py)]=[floor,1,ceiling-floor]
                else:
                    previous[0]+=floor
                    previous[1]+=1
                    previous[2]=min(previous[2],ceiling-floor)
    return ({position:(row[0]/row[1],row[2]) for position,row in pixels.items()},
            count,stacked)


def _mix(a,b,t):
    return tuple(round(x+(y-x)*t) for x,y in zip(a,b))


def _draw_surface(canvas, origin, surface, range_y, cell_size):
    ox,oy=origin
    lo,hi=range_y
    palette=PALETTE
    for (x,y),(floor,clearance) in surface.items():
        t=max(0,min(1,(floor-lo)/max(1,hi-lo)))
        color=_mix(palette['floor_low'],palette['floor_high'],t)
        neighbours=[surface.get((x+dx,y+dy)) for dx,dy in ((-1,0),(1,0),(0,-1),(0,1))]
        if any(neighbour is None for neighbour in neighbours):
            color=_mix(color,palette['edge'],.62)
        else:
            # A restrained relief accent; 5 m contours follow sampled geometry.
            relief=max(-8,min(8,(neighbours[0][0]-neighbours[2][0])*cell_size))
            color=tuple(max(0,min(255,round(channel+relief))) for channel in color)
            level=math.floor(floor*cell_size/5)
            if any(math.floor(n[0]*cell_size/5)!=level for n in neighbours[:3:2]):
                color=_mix(color,palette['edge'],.2)
        canvas.rect(ox+x,oy+y,1,1,color)


def _marker_visible(marker, panel, panel_index):
    if 'panels' in marker:
        return panel_index in marker['panels']
    y=marker.get('y_cells')
    return y is None or panel['range_y'][0]<=y<panel['range_y'][1]


def _draw_marker(canvas,x,y,kind,label,box):
    color=PALETTE.get(kind,PALETTE['checkpoint'])
    canvas.circle(x,y,8,PALETTE['panel'])
    canvas.circle(x,y,6,color)
    if kind=='entrance':
        canvas.rect(x-2,y-3,4,7,PALETTE['panel'])
        canvas.rect(x,y,3,1,color)
    elif kind=='exit':
        canvas.line(x-3,y,x+3,y,PALETTE['panel'])
        canvas.line(x+1,y-2,x+3,y,PALETTE['panel'])
        canvas.line(x+1,y+2,x+3,y,PALETTE['panel'])
    else:
        canvas.circle(x,y,2,PALETTE['panel'])
    if label and kind!='portal':
        label=str(label).upper()
        label_width=_text_width(label,2)
        left=x+13
        if left+label_width+5>box[2]:
            left=x-13-label_width
        left=max(box[0]+3,min(left,box[2]-label_width-3))
        desired_top=y-29 if kind in ('entrance','exit') else y-7
        top=max(box[1]+3,min(desired_top,box[3]-17))
        canvas.rect(left-3,top-3,label_width+6,20,PALETTE['panel'])
        canvas.text(left,top,label,color,2)
        return [left-3,top-3,left+label_width+3,top+17]
    return None


def render_atlas(path, hallname, bounds, panels, markers=(), *, build='26430625',
                 cell_size=.5, pixels_per_metre=1.0, columns=None,
                 note='Geometry-derived map | floors grouped by elevation'):
    """Write a fresh, independently decoded PNG and return its geometry receipt.

    ``bounds`` and optional per-panel bounds use an exclusive maximum. All
    panels share the requested physical pixel scale even when independently
    cropped. Markers contain continuous world coordinates divided by
    ``cell_size`` as ``gx``, ``gz``, optional ``y_cells``, ``kind``
    (entrance/exit/checkpoint/portal), ``label`` and optional zero-based ``panels``.
    Unlike sample indices, marker coordinates are exact scene positions.
    Elevation ranges are half-open intervals in cells, not metres. The returned
    panel transforms specify the origin and physical scale for offline checks.
    """
    path=Path(path)
    if path.exists():
        raise ValueError('Use a fresh atlas filename')
    bounds=_bounds(bounds)
    if not panels:
        raise ValueError('At least one elevation panel is required')
    if not (0<cell_size<=10 and 0<pixels_per_metre<=8):
        raise ValueError('Invalid cell size or pixel scale')
    panels=list(panels)
    markers=list(markers)
    ppc=cell_size*pixels_per_metre
    prepared=[]
    for index,panel in enumerate(panels):
        band=panel.get('range_y',())
        if (len(band)!=2 or any(not isinstance(v,(float,int)) or not math.isfinite(v) for v in band)
                or band[1]<=band[0]):
            raise ValueError('Panel elevation must be a positive half-open interval')
        panel_bounds=_bounds(panel.get('bounds',bounds))
        w,h,_=_projection(panel_bounds,ppc)
        label=panel.get('label',f'ELEVATION {band[0]*cell_size:g}-{band[1]*cell_size:g} M')
        prepared.append({'index':index,'source':panel,'bounds':panel_bounds,
                         'map_size':[w,h],'label':label})
    pad,gap=24,18
    panel_width=max(440,max(row['map_size'][0] for row in prepared)+pad*2)
    panel_height=max(row['map_size'][1] for row in prepared)+145
    if columns is None:
        columns=min(3,max(1,math.ceil(math.sqrt(len(panels)*panel_height/panel_width))))
    if type(columns) is not int or not 1<=columns<=len(panels):
        raise ValueError('Columns must be a positive integer no larger than the panel count')
    rows=math.ceil(len(panels)/columns)
    width=max(960,pad*2+columns*panel_width+(columns-1)*gap)
    title_lines=_wrap(hallname,width-2*pad-110,3)
    header_height=34+len(title_lines)*29+44
    footer_lines=_wrap(note,width-pad*2,2)
    footer_height=91+len(footer_lines)*21
    height=header_height+rows*panel_height+(rows-1)*gap+footer_height
    if max(width,height)>16000 or width*height>80_000_000:
        raise ValueError('Atlas exceeds safe raster dimensions; crop panels or reduce the pixel scale')
    canvas=_Canvas(width,height,PALETTE['paper'])
    for i,line in enumerate(title_lines):
        canvas.text(pad,26+i*29,line,scale=3)
    subtitle=f'HOLLOW HALLS  /  {len(panels)} ELEVATION PANELS'
    canvas.text(pad,34+len(title_lines)*29,subtitle,PALETTE['muted'],2)
    canvas.line(pad,header_height-14,width-pad,header_height-14,PALETTE['border'])
    # The compass describes the game world X/Z projection used in every panel.
    cx,cy=width-65,43
    canvas.text(cx-5,cy-21,'N',scale=2)
    canvas.line(cx,cy+23,cx,cy,PALETTE['ink'],2)
    canvas.line(cx,cy,cx-7,cy+13,PALETTE['ink'],2)
    canvas.line(cx,cy,cx+7,cy+13,PALETTE['ink'],2)
    receipts=[]
    grid_step=max(1,round(25/cell_size))
    for row in prepared:
        index=row['index']
        panel=row['source']
        column=index%columns
        line=index//columns
        left=pad+column*(panel_width+gap)
        top=header_height+line*(panel_height+gap)
        canvas.rect(left,top,panel_width,panel_height,PALETTE['panel'])
        canvas.rect(left,top,panel_width,3,PALETTE['border'])
        label_lines=_wrap(row['label'],panel_width-pad*2,3)
        if len(label_lines)>2:
            raise ValueError('Panel label exceeds the two-line header')
        for j,label in enumerate(label_lines):
            canvas.text(left+pad,top+18+j*27,label,scale=3)
        altitude=f'Y {panel["range_y"][0]*cell_size:g}-{panel["range_y"][1]*cell_size:g} M'
        canvas.text(left+pad,top+75,altitude,PALETTE['muted'],1)
        w,h=row['map_size']
        ox=left+(panel_width-w)//2
        oy=top+98
        bx,bz,ex,ez=row['bounds']
        for gx in range(math.ceil(bx/grid_step)*grid_step,ex,grid_step):
            x=ox+(gx-bx)*ppc
            canvas.line(x,oy,x,oy+h-1,PALETTE['grid'])
        for gz in range(math.ceil(bz/grid_step)*grid_step,ez,grid_step):
            y=oy+(ez-gz)*ppc
            canvas.line(ox,y,ox+w-1,y,PALETTE['grid'])
        surface,native_samples,stacked=_surface(panel['samples'],row['bounds'],ppc)
        _draw_surface(canvas,(ox,oy),surface,panel['range_y'],cell_size)
        _,_,position=_projection(row['bounds'],ppc)
        placed=[]
        for marker in markers:
            if not _marker_visible(marker,panel,index):
                continue
            gx,gz=marker['gx'],marker['gz']
            if not (bx<=gx<ex and bz<=gz<ez):
                continue
            # The projection helper centres voxel sample indices; scene marker
            # coordinates already denote continuous world positions.
            px,py=position(gx-.5,gz-.5)
            x,y=ox+math.floor(px),oy+math.floor(py)
            kind=marker.get('kind','checkpoint')
            label='' if kind=='portal' else marker.get('label','')
            label_box=_draw_marker(canvas,x,y,kind,label,
                                   (left,top+52,left+panel_width,top+panel_height-28))
            placed.append({'kind':kind,'label':label,'world_cells':[gx,gz],
                           'pixel':[x,y],'label_box_pixels':label_box})
        detail=f'{(ex-bx)*cell_size:g} X {(ez-bz)*cell_size:g} M'
        canvas.text(left+pad,top+panel_height-27,detail,PALETTE['muted'],2)
        receipts.append({'index':index,'label':row['label'],'range_y_cells':list(panel['range_y']),
                         'bounds_cells':row['bounds'],'map_origin_pixels':[ox,oy],
                         'map_dimensions_pixels':[w,h],'pixels_per_metre':pixels_per_metre,
                         'source_samples':native_samples,'raster_surface_pixels':len(surface),
                         'stacked_column_samples':stacked,'markers':placed})
    footer_y=header_height+rows*panel_height+(rows-1)*gap+22
    # A physical scale bar is shared by every elevation panel, irrespective of crop.
    target=120/pixels_per_metre
    length=next((n for n in (100,50,25,10,5,1) if n<=target),1)
    bar_width=round(length*pixels_per_metre)
    for segment in range(4):
        x0=pad+round(segment*bar_width/4)
        x1=pad+round((segment+1)*bar_width/4)
        canvas.rect(x0,footer_y,x1-x0,7,PALETTE['ink'] if segment%2==0 else PALETTE['border'])
    canvas.text(pad+bar_width+12,footer_y-3,f'{length} M',PALETTE['muted'],2)
    legend_x=pad+bar_width+110
    for key,label in (('entrance','ENTRANCE'),('exit','EXIT'),('checkpoint','CHECKPOINT'),('portal','PORTAL')):
        if any(marker.get('kind','checkpoint')==key for marker in markers):
            canvas.circle(legend_x+5,footer_y+3,5,PALETTE[key])
            canvas.text(legend_x+17,footer_y-3,label,PALETTE['muted'],2)
            legend_x+=_text_width(label,2)+43
    for i,line in enumerate(footer_lines):
        canvas.text(pad,footer_y+28+i*21,line,PALETTE['muted'],2)
    canvas.text(pad,height-24,f'ENSHROUDED  |  BUILD {build}  |  NORTH = +Z  /  EAST = +X',PALETTE['muted'],1)
    digest=hashlib.sha256(canvas.rgb).hexdigest()
    path.parent.mkdir(parents=True,exist_ok=True)
    png_from_rgb(path,width,height,
                 (memoryview(canvas.rgb)[y*width*3:(y+1)*width*3] for y in range(height)))
    check=verify_png(path,digest)
    return {'path':str(path),'dimensions':[width,height],'rgb_sha256':digest,
            'png_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
            'png_verification':check,'cell_size_metres':cell_size,
            'pixels_per_metre':pixels_per_metre,'columns':columns,'panels':receipts,
            'scale_bar_metres':length,'palette':{key:list(value) for key,value in PALETTE.items()},
            'orientation':'North is +Z; east is +X. Row zero is north.',
            'representation':'Geometry-derived floor surfaces, grouped by half-open elevation intervals.'}
