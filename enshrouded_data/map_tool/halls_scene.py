"""Decode packed scene positions and indexed entity spawn overrides."""

import math




def position_world(transform):
    """Convert paired uint16 words from signed 16.16 fixed point to metres."""
    result={}
    for axis in 'XYZ':
        low,high=transform['position'+axis+'0'],transform['position'+axis+'1']
        if not(0<=low<=65535 and 0<=high<=65535):
            raise ValueError('Packed scene position word outside uint16')
        value=low|(high<<16)
        if value>=2**31:value-=2**32
        result[axis.lower()]=value/65536
    return result


def spawn_overrides(data,index):
    """Decode component and masked overrides, rejecting unsupported layouts."""
    if index<0:
        raise ValueError('Negative scene entity index')
    if index>=len(data['entries']):
        return {'components':[],'component_start':0,'component_count':0,'spawn_mask':[]}
    entry=data['entries'][index]
    start=entry['spawnIndexStart']
    end=data['entries'][index+1]['spawnIndexStart'] if index+1<len(data['entries']) else len(data['indices'])
    if not 0<=start<end<=len(data['indices']):
        raise ValueError('Invalid scene spawn index range')
    raw=bytes(data['indices'][start:end]);cursor=0
    def integer(size):
        nonlocal cursor
        if cursor+size>len(raw):raise ValueError('Truncated scene spawn indices')
        value=int.from_bytes(raw[cursor:cursor+size],'little');cursor+=size;return value
    # Component count precedes its optional start index and masked array indices.
    count=integer(1)
    first=integer(2) if count else 0
    if first+count>len(data['components']):raise ValueError('Scene component override range outside array')
    result={'components':data['components'][first:first+count],
        'component_start':first,'component_count':count,'spawn_mask':entry['spawnMask']}
    flags=set(entry['spawnMask']);handled=set()
    def selection(flag_names,field,size):
        present=flags&set(flag_names)
        if not present:return
        handled.update(present);selected=integer(size)
        if selected>=len(data[field]):raise ValueError('Scene spawn index outside '+field)
        result[field]={'index':selected,'value':data[field][selected]}
    # Masked fields are serialized in this order; grouped flags share one index.
    selection(['TriggerRange','TriggerRange2','TriggerRange3'],'triggerRanges',2)
    selection(['EnemySettings'],'enemySettings',1)
    selection(['RandomSpawnerSetting_AllowUpScaling','RandomSpawnerSetting_AllowDownScaling','RandomSpawnerSetting_MinSpawnerAmount'],'randomSpawnerSettings',1)
    selection(['Level'],'levels',1)
    selection(['EnemyMarkerTag'],'enemyMarkerTags',1)
    # Teleporter identity and destination are separate indices into locaTags.
    for flag in ['TeleporterId','TeleportTargetId']:
        if flag in flags and flag not in handled:
            selected=integer(1);handled.add(flag)
            if selected>=len(data['locaTags']):raise ValueError('Teleporter loca index outside array')
            result[flag]={'index':selected,'value':data['locaTags'][selected]}
    selection(['JumpParameters'],'jumps',1)
    selection(['EnemyTestFlags','NpcTestFlags','AnimalTestFlags'],'testFlags',1)
    if flags!=handled:raise ValueError('Unknown scene spawn flags: '+str(flags-handled))
    if cursor!=len(raw):raise ValueError('Unconsumed scene spawn index bytes')
    return result

