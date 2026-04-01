# Half Sword Online

**This is a work in progress. Nothing is tested. Don't expect it to work yet.**

Online multiplayer for [Half Sword](https://store.steampowered.com/app/2397300/Half_Sword/) via viewport streaming. Up to 8 players. Only the host needs the game.

## Idea

Spawn extra players in-game via UE4SS, capture each player's camera to a render target, encode with NVENC, stream over UDP. Clients send input back as virtual gamepads. No netcode replication — pure video streaming.

```
Host: Game → SceneCapture2D → GPU readback → NVENC → UDP → Client
Client: UDP → decode → display | input → UDP → Host → virtual gamepad → Game
```

## Status

Everything is scaffolded, nothing is end-to-end tested.

See [issues](https://github.com/NodeNestor/half-sword-online/issues) for what needs doing.

## License

MIT
