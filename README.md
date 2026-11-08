# Spatial-Lab

An interactive 3D test bench for a multichannel room. Open a page, drag a sound object around a model of
the room, and hear it move on the real speakers. Built for a 5.1.2 setup (Logitech Z906 bed, two ceiling
height speakers, 7.1 USB interface, CamillaDSP, PipeWire), but the layout is a file you can change.

It demonstrates:

- **Panning** in 3D (VBAP over the real speaker directions, or nearest-speaker for comparison), including height.
- **Distance cues**: level, air absorption and optional Doppler.
- **Phantom imaging** ("phasing"): per-speaker level, delay (0 to 50 ms) and polarity, with notes on summing
  localisation, the precedence effect and comb filtering.
- **Image width**: correlated vs decorrelated copies of the signal across the speakers.
- **Paths**: nine presets, drag-recording with replay, and a speed control.
- **Speaker identification**: plays each speaker in turn so you can prove the channel map.

Sound is made by a local Python engine, not by the browser. The page only draws and sends commands.

## Safety

- Starts **muted**. Unmute fades in over 3 s.
- Master level is clamped to a **ceiling of -40 dBFS** read from your config at start-up. The page cannot raise it.
  Raise it by hand: `spatial-lab set-ceiling -30`, then restart.
- **Esc** or the PANIC button silences in 20 ms. **M** toggles mute.
- The engine mutes itself if the page stops responding for 4 s or the last page closes.
- The LFE output is always silent.
- The server listens on 127.0.0.1 only and rejects wrong Host/Origin headers.

Start with the amplifier volume low and raise the ceiling in small steps.

## Install

```
sudo apt install python3-numpy python3-scipy python3-aiohttp python3-yaml ffmpeg pipewire-bin
git clone https://github.com/praveenchandrahas/Spatial-Lab && cd Spatial-Lab
python3 -m spatial_lab check          # what it can see
python3 -m spatial_lab serve --open   # then use the page
```

(`pip install -e .` also works and adds the `spatial-lab` command. `python3-matplotlib` adds plots to `render`.)

## First run checklist

1. `spatial-lab check`: the USB sink should show `present`; CamillaDSP `present` if you want Through mode.
   Stop mpv or anything else playing first.
2. Start with **Direct** and a low master. Press **Unmute**.
3. Set mode **Identify**: each speaker should sound in turn, in the order FL, FR, TFL, TFR, C, SL, SR.
   Check that the height speakers are the ceiling ones. If an order is wrong, edit the layout file's `port` values.
4. Back to **Object** mode, press **Front centre**, then play a path.
5. Switch to **Through CamillaDSP** and compare. Expect differences: CamillaDSP adds bass management, EQ, its own
   delays and trims. In Through mode distance alignment is off by default because CamillaDSP already aligns.

## Using the page

- Drag the amber ball. Shift+drag (or the "Drag up/down" box, key V) moves it vertically.
- Keys: M mute, Esc panic, Space path, arrows move front/back/left/right, PageUp/PageDown up/down,
  1 to 4 cameras, I identify.
- **Chosen speakers** mode plays one signal from the speakers you pick. Use trim, delay and polarity per speaker
  and the Labs buttons to hear phantom images, precedence and comb filtering.
- Source: pink noise (default, high-passed at 200 Hz so the satellites get no bass) or any audio file ffmpeg can read.

## Offline renders

`spatial-lab render --path lap --out renders` writes an 8-channel WAV, one WAV per speaker and a plot of intended
vs predicted image direction and speaker gains. Nothing is played.

## What has and has not been tested

Tested (automated, 30 tests, plus a headless browser run):

- VBAP maths, pink-noise spectrum, fractional delay, decorrelation, level ceiling, fade-in, panic, watchdog,
  LFE silence, polarity flip, delay, alignment ordering, distance falloff, identify cycling.
- Server security (Host/Origin rejection, path traversal), uploads, recordings, commands.
- The page loads, connects, shows live readouts, and dragging moves the object.

**Not tested** (needs your machine):

- Real audio through PipeWire: `pw-cat` start-up, the explicit links to the USB sink and to CamillaDSP, and the
  route switch. The code follows the pattern of your tone-test script, but it has never run against your system.
- Whether anything sounds right. All "predicted image" values are calculated, not measured.
- Mic-based verification is deferred.

## Known limitations and assumptions

- **Surround distance.** You gave about 3 ft to each surround. Your CamillaDSP delay and trim for them suggest
  nearer 5 ft. The layout file uses 3 ft; measure and correct it. It only affects the 3D drawing and alignment.
- **Heights' distance, ceiling height and seat-to-back-wall are guesses** (marked `provisional`).
- Both height speakers are ahead of the seat, so an object straight overhead lands on TFL+TFR plus the surrounds.
  Height is level-only: there are no spectral height cues.
- Air absorption and Doppler are nearly inaudible at room scale. The multiplier controls exist so you can hear them.
- The Z906 still sends bass from the satellites to its sub, so the 200 Hz high-pass on the source matters.
- A browser tab left in the background for several minutes may have its timers throttled enough to trip the
  watchdog. The result is a mute, never a loud sound.

## Layout and config

- Layout: `spatial_lab/layouts/praveen-5.1.2.yml`; override with `~/.config/spatial-lab/layout.yml`,
  `$SPATIAL_LAB_LAYOUT` or `--layout`.
- Config: `~/.config/spatial-lab/config.yml` (ceiling, fade-in, port, high-pass).
- Recordings: `~/.local/share/spatial-lab/recordings`.

See `docs/design.md` for how it works. Licence: Apache-2.0. The vendored three.js is MIT (see `spatial_lab/web/vendor`).
