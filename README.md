# Half Sword Online

> **Status: Early development. Nothing is tested yet. This project just started.**

Online multiplayer mod for [Half Sword](https://store.steampowered.com/app/2397300/Half_Sword/). Up to 8 players. Co-op, PvP, team battles.

Only the **host** needs the game. Clients just run a lightweight app and see a video stream of their player's viewport.

## How it works

The host runs Half Sword with a UE4SS mod that spawns extra players, each with their own camera. A C++ plugin reads each camera's rendered frame from the GPU into shared memory. A Python process encodes those frames with NVENC and streams them over UDP to remote clients. Clients send their input back, which gets injected as virtual Xbox controllers via ViGEmBus.

```
Host: Game → SceneCapture2D → GPU readback → NVENC encode → UDP → Client
Client: UDP → decode → display | input → UDP → Host → virtual gamepad → Game
```

## Project structure

```
mod/HalfSwordOnlineMod/   UE4SS Lua mod — player spawning, camera setup, teams
mod/FrameExport/           UE4SS C++ plugin — GPU readback to shared memory
host/                      Python host server — encoding, streaming, input injection
client/                    Python client app — decoding, display, input capture
shared/                    Protocol, FEC, lobby system
```

## What works (in theory)

- [x] Network protocol with video fragmentation and FEC
- [x] NVENC encoding pipeline (FFmpeg)
- [x] Virtual gamepad input injection (ViGEmBus)
- [x] Client connect screen with LAN auto-discovery
- [x] Lobby with team selection (Allies/Enemies/FFA/Teams)
- [x] Host dashboard (tkinter)
- [x] Adaptive bitrate control
- [x] Audio capture (WASAPI) and playback (Opus)
- [x] UE4SS Lua mod for player spawning + SceneCapture2D
- [x] C++ plugin for GPU readback to shared memory
- [x] GitHub Actions CI + release builds

## What needs testing / finishing

- [ ] Actually test with Half Sword (need to verify UE4SS class names with LiveView)
- [ ] C++ plugin GPU readback (`ReadPixels` via UFunction — may need vtable approach)
- [ ] SceneCapture2D creation from Lua (`StaticConstructObject` behavior varies)
- [ ] End-to-end stream: capture → encode → network → decode → display
- [ ] Input injection feels right (mouse sensitivity, gamepad mapping)
- [ ] Multi-player stress test (4+ players)
- [ ] Audio sync with video
- [ ] Package client as standalone .exe

## Requirements

### Host
- Half Sword (Steam Early Access or Demo)
- [UE4SS experimental-latest](https://github.com/UE4SS-RE/RE-UE4SS/releases) (needs UE 5.4 support)
- [ViGEmBus](https://github.com/nefarius/ViGEmBus/releases)
- FFmpeg with NVENC
- Python 3.10+
- NVIDIA GPU (for NVENC encoding)

### Client
- FFmpeg
- Python 3.10+ (or the packaged .exe when available)

## Quick start

### Host
```bash
# Install UE4SS experimental-latest into Half Sword
# Copy mod/HalfSwordOnlineMod/ into <game>/Binaries/Win64/Mods/
# Add "HalfSwordOnlineMod : 1" to Mods/mods.txt

pip install vgamepad
python -m host.server --port 8080
```

### Client
```bash
pip install pygame
python -m client --host <IP>:8080 --name "Player2"

# Or just run: python -m client
# (opens a GUI where you can enter the host IP)
```

Default port is **8080**. Change with `--port`.

## Game modes

Players pick their team in the lobby. No auto-detection, no map dependency.

| Choice | Effect |
|--------|--------|
| Allies | Same team as host (co-op vs AI) |
| Enemies | Opposing team |
| FFA | Free for all |
| Team A / B | Custom team splits |

## Future

- Room codes for easy internet connections (no port forwarding)
- Standalone .exe builds via GitHub releases
- Web-based client (WebRTC)
- Spectator mode

## License

MIT

## Credits

- [HalfSwordSplitScreenMod](https://github.com/massclown/HalfSwordSplitScreenMod) — original split-screen logic
- [HalfSwordModdingResources](https://github.com/massclown/HalfSwordModdingResources) — modding reference
- [UE4SS](https://github.com/UE4SS-RE/RE-UE4SS) — Unreal Engine scripting
- [Sunshine](https://github.com/LizardByte/Sunshine) — streaming architecture reference
