-- Half Sword Online — Mod Configuration
-- Edit these values to customize behavior.
-- The host server can also override some of these via the bridge.

local Config = {}

-- Maximum remote players (host is always player 1)
Config.MaxRemotePlayers = 7

-- Default capture resolution for each remote player's SceneCapture2D
Config.DefaultCaptureWidth = 1920
Config.DefaultCaptureHeight = 1080

-- Target capture FPS (scene capture updates per second)
Config.DefaultCaptureFPS = 60

-- Gamepad offset: true = first physical gamepad controls Player 2
-- (host uses keyboard/mouse)
Config.OffsetGamepad = true

-- Splitscreen layout (only used internally, overridden to fullscreen)
-- 0 = horizontal (top/bottom), 1 = vertical (left/right)
Config.SplitscreenLayout = 1

-- Bridge polling: how often (in frames) to check for host commands
Config.BridgePollInterval = 30

-- Delay before setting up scene capture after player spawn (ms)
-- Allows the pawn to fully initialize
Config.SceneCaptureSetupDelay = 500

-- Delay before overriding viewport after player creation (ms)
Config.ViewportOverrideDelay = 100

-- Scene capture settings
Config.SceneCapture = {
    -- What to capture (matches ESceneCaptureSource enum)
    -- 0 = SCS_FinalColorLDR (post-processing included, what player sees)
    -- 1 = SCS_FinalColorHDR
    -- 5 = SCS_SceneColorHDR (no post-processing)
    CaptureSource = 0,

    -- Field of view (degrees). 0 = match player camera FOV dynamically
    FOVAngle = 0,

    -- Capture every frame (true) or only on movement (false)
    CaptureEveryFrame = true,
}

-- Default game mode for remote players
-- "coop"  = all players on Team 1 (vs AI)
-- "pvp"   = all players on Team 0 (free-for-all)
-- "teams" = split into Team 1 vs Team 2
-- "auto"  = detect from current game mode
Config.DefaultGameMode = "coop"

-- Team Int values (matches the game's internal team system)
Config.Teams = {
    FFA = 0,       -- Free for all (attacks everyone)
    Player = 1,    -- Player/ally team
    Enemy = 2,     -- Enemy/NPC team
}

return Config
