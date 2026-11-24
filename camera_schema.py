"""Per-recording camera identities, shared by recognition and timeline export."""
from string import ascii_uppercase

DEFAULT_CAMERAS = ('A', 'B', 'C', 'D')
MAX = 26
MAX_CAMERAS = MAX


def cameras_for(config):
    """Return ordered, unique camera IDs; legacy projects default to A/B/C/D."""
    if 'camera_ids' in config:
        values = config['camera_ids']
        if not isinstance(values, (list, tuple)) or not 1 <= len(values) <= MAX:
            raise ValueError('机位列表必须包含 1～26 个机位。')
        if (any(not isinstance(value, str) or len(value) != 1 or value not in ascii_uppercase
                for value in values) or len(set(values)) != len(values)):
            raise ValueError('机位标识须为不重复的单个大写字母 A～Z。')
        return tuple(values)
    if 'camera_count' in config:
        count = config['camera_count']
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= MAX:
            raise ValueError('机位数量必须为 1～26 的整数。')
        return tuple(ascii_uppercase[:count])
    return DEFAULT_CAMERAS


def active_label_cameras(config):
    return tuple(camera for camera in cameras_for(config)
                 if camera != 'A' or not config.get('infer_a_when_blank', False))
