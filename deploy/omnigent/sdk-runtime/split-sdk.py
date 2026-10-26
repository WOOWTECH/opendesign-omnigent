"""Keep every SDK COPY layer below the registry ingress upload limit."""
from pathlib import Path
import shutil

source = Path('/opt/omnigent')
layers = [Path('/opt/omnigent-layers') / str(i) for i in range(8)]
for layer in layers:
    layer.mkdir(parents=True, exist_ok=True)
index, size = 0, 0
limit = 64 * 1024 * 1024
for item in sorted(source.rglob('*')):
    if item.is_dir() and not item.is_symlink():
        continue
    count = item.lstat().st_size
    assert count < limit, 'SDK file exceeds upload layer budget'
    if size + count > limit:
        index += 1
        size = 0
    assert index < len(layers), 'SDK exceeds bounded layer count'
    target = layers[index] / item.relative_to('/')
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(item, target, follow_symlinks=False)
    size += count
print('SDK split into bounded registry layers')
