# Spatial-Lab design notes

## Shape

```
browser (Three.js page)  <-- JSON over WebSocket (127.0.0.1) -->  Python engine  --pw-cat--> PipeWire
   draws the room, drag, UI                                         VBAP, delays, decorrelation      |
                                                                                         direct: USB interface
                                                                                         through: camilladsp-capture
```

The page makes no sound. It sends the object position and control changes; the engine renders 8 channels in
512-frame blocks at 48 kHz and pipes them to `pw-cat` (raw float32, 8 channels, channel order FL FR RL RR FC LFE SL SR,
the interface's own order). The engine pushes meters and the current object position back about 25 times a second.

## Coordinates

x right, y front, z up, metres, origin at the listening position at ear height. Three.js gets (x, z, -y).

## Panning

VBAP over the convex hull of the speaker unit vectors plus an imaginary speaker straight down, so the sphere is
covered. The object direction picks a triangle; the three gains are normalised to constant power. Source size
averages the gains over a cone of directions. "Nearest" gives all the signal to the closest speaker.
The predicted image direction on screen is the energy vector (and velocity vector) of the gains. It is a model
of localisation, not a measurement.

## Distance and extras

Level follows `1/r^exponent` (exponent 0.5 by default; 1 is free field). Air absorption is a one-pole low-pass
whose corner follows distance, with an exaggeration factor. Doppler is a delay that changes during each block.
Optional alignment (Direct mode) delays and attenuates the nearer speakers so all arrive together.

## Image width and phase

Decorrelation convolves each speaker feed with its own random-phase FIR (2048 taps, ~12 ms), mixed in by the
Width control. Per-speaker trim, delay (0 to 50 ms) and polarity allow the precedence, summing-localisation,
comb-filtering and out-of-phase demonstrations by hand.

## Safety

Starts muted. Master level is clamped to a ceiling read from the config file at start; the page cannot change it.
Unmute fades in over 3 s. Level changes are slew-limited (12 dB/s up, 60 dB/s down). Mute takes 0.12 s, panic 0.02 s.
The engine mutes if the page stops sending its heartbeat for 4 s or the last page closes. The LFE output is
always silent. Output is hard-clipped at full scale as a last resort.

## Server

aiohttp on 127.0.0.1 only. Every request needs a Host of `127.0.0.1:<port>` or `localhost:<port>`; WebSocket and POST
also need a matching Origin, which stops another web page from driving the amplifier. The CSP allows no inline
script. Uploads are decoded with ffmpeg to 48 kHz mono (first five minutes).

## Layout file

`spatial_lab/layouts/author-5.1.2.yml` holds the output port order, CamillaDSP node names, room size and each
speaker's direction and rough distance. Copy it to `~/.config/spatial-lab/layout.yml` to override it. Entries
marked `provisional` are guesses.

See the [screenshots in the README](index.md#screenshots) for renders of the page (silent null backend).
