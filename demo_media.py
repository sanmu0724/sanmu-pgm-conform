"""Synthetic source images and video writer for the PGM demo."""
import av
import numpy as np
from PIL import Image,ImageDraw
FPS=25

def scene(cam, n, width=400, height=224):
    colors = {'A': (85, 60, 30), 'B': (35, 95, 55), 'C': (80, 35, 95), 'D': (35, 70, 115)}
    frame = np.full((height, width, 3), colors[cam], dtype=np.uint8)
    # Include a contrasting object in the synthetic stage image.
    image = Image.fromarray(frame)
    draw = ImageDraw.Draw(image)
    draw.rectangle((width//3, height//3, width//2, height//2), fill=(0,0,240))
    x, y = 20+(n*3) % (width-40), height//2
    draw.ellipse((x-16,y-16,x+16,y+16),fill=(230,230,230))
    draw.text((12,height-25),f'{cam} / frame {n:03}',fill=(255,255,255))
    return np.asarray(image).copy()

def write_video(path, frames, tc):
    with av.open(str(path), 'w') as out:
        stream = out.add_stream('libx264', rate=FPS)
        first = next(frames)
        stream.width, stream.height = first.shape[1], first.shape[0]
        stream.pix_fmt = 'yuv420p'
        stream.options = {'crf': '16', 'preset': 'ultrafast'}
        stream.metadata['timecode'] = tc
        from itertools import chain
        for image in chain([first], frames):
            frame = av.VideoFrame.from_ndarray(image, format='bgr24')
            for packet in stream.encode(frame): out.mux(packet)
        for packet in stream.encode(): out.mux(packet)
