-- Half Sword Online Mod
-- N-player remote play via per-player SceneCapture2D → GPU readback → stream
-- Requires: UE4SS experimental-latest (UE 5.4+ support)
--
-- Architecture:
--   1. Spawn N local players via UGameplayStatics::CreatePlayer
--   2. Each remote player gets a SceneCaptureComponent2D attached to their camera
--   3. Each SceneCapture renders to its own TextureRenderTarget2D at full res
--   4. The C++ native plugin (FrameExport) reads render targets → shared memory
--   5. Python host sidecar reads shared memory → NVENC encode → stream to clients
--   6. Remote input comes back via ViGEmBus virtual gamepads
--
-- The host player (Player 1) plays normally — their viewport is unaffected.
-- Remote players' viewports are captured offscreen via SceneCapture2D.

local UEHelpers = require("UEHelpers")
local Config = require("config")
local TeamManager = require("team_manager")

local GetGameplayStatics = UEHelpers.GetGameplayStatics
local GetGameMapsSettings = UEHelpers.GetGameMapsSettings
local GetWorldContextObject = UEHelpers.GetWorldContextObject

-------------------------------------------------------------------------------
-- State
-------------------------------------------------------------------------------

local Players = {}
-- Players[slot] = {
--     controller     : PlayerController
--     pawn           : Character pawn
--     sceneCapture   : SceneCaptureComponent2D (attached to camera)
--     renderTarget   : TextureRenderTarget2D
--     captureWidth   : int
--     captureHeight  : int
-- }

local HostPlayerController = nil
local IsInitialized = false

-------------------------------------------------------------------------------
-- Logging
-------------------------------------------------------------------------------

local MOD = "HalfSwordOnline"
local function Log(msg) print(string.format("[%s] %s", MOD, msg)) end
local function Logf(fmt, ...) print(string.format("[%s] %s", MOD, string.format(fmt, ...))) end
local function ErrLog(msg) print(string.format("[%s] [ERROR] %s", MOD, msg)) end

-------------------------------------------------------------------------------
-- Player Controller Cache
-------------------------------------------------------------------------------

local function CachePlayerControllers()
    local all = FindAllOf("PlayerController")
    if not all then return {} end
    local result = {}
    for _, pc in pairs(all) do
        if pc:IsValid() and pc.Player and pc.Player:IsValid()
            and not pc:HasAnyInternalFlags(EInternalObjectFlags.PendingKill) then
            result[pc.Player.ControllerId + 1] = pc
        end
    end
    return result
end

local function GetPlayerPawn(controller)
    if controller and controller:IsValid() and controller.Pawn and controller.Pawn:IsValid() then
        return controller.Pawn
    end
    return nil
end

-------------------------------------------------------------------------------
-- Input Remapping
-- Map keyboard actions → gamepad so virtual controllers work for all actions
-------------------------------------------------------------------------------

local GamepadKeyMap = {
    ["InpActEvt_Q_K2Node_InputKeyEvent_4"] = "Gamepad_LeftShoulder",
    ["InpActEvt_Q_K2Node_InputKeyEvent_5"] = "Gamepad_LeftShoulder",
    ["InpActEvt_E_K2Node_InputKeyEvent_2"] = "Gamepad_RightShoulder",
    ["InpActEvt_E_K2Node_InputKeyEvent_3"] = "Gamepad_RightShoulder",
    ["InpActEvt_X_K2Node_InputKeyEvent_8"] = "Gamepad_FaceButton_Left",
}

local function RemapInputBindingsForGamepad()
    local allBindings = FindAllOf("InputKeyDelegateBinding")
    if not allBindings then return end
    for _, binding in pairs(allBindings) do
        if binding and binding:IsValid() then
            local arr = binding['InputKeyDelegateBindings']
            if arr then
                arr:ForEach(function(index, element)
                    local b = element:get()
                    local funcName = b['FunctionNameToBind']:ToString()
                    local gpKey = GamepadKeyMap[funcName]
                    if gpKey then
                        b["InputChord"]["Key"]["KeyName"] = FName(gpKey)
                        element:set({
                            FunctionNameToBind = b['FunctionNameToBind'],
                            InputKeyEvent = b['InputKeyEvent'],
                            InputChord = b['InputChord']
                        })
                    end
                end)
            end
        end
    end
    Log("Remapped input bindings for gamepad")
end

-------------------------------------------------------------------------------
-- HUD Management
-------------------------------------------------------------------------------

local function DismissDeathScreen()
    local DED = FindFirstOf("UI_DED_C")
    if DED and DED:IsValid() then
        DED:RemoveFromViewport()
        Log("Dismissed death screen")
        if GetGameplayStatics():IsGamePaused(GetWorldContextObject()) then
            GetGameplayStatics():SetGamePaused(GetWorldContextObject(), false)
            Log("Unpaused after death screen")
        end
    end
end

-------------------------------------------------------------------------------
-- Render Target Creation
-- Creates a TextureRenderTarget2D that SceneCapture2D will render into.
-- The C++ FrameExport plugin reads from these render targets.
-------------------------------------------------------------------------------

local function CreateRenderTarget(width, height, slot)
    Logf("Creating RenderTarget2D %dx%d for slot %d", width, height, slot)

    -- NOTE: StaticConstructObject usage needs testing — may not be available
    -- or may behave differently across UE4SS versions.
    local rtClass = StaticFindObject("/Script/Engine.TextureRenderTarget2D")
    if not rtClass then
        -- Alternative: find by short name
        ErrLog("Could not find TextureRenderTarget2D class via StaticFindObject")
        ErrLog("Trying NewObject approach...")
        return nil
    end

    -- Create via NewObject pattern
    -- In UE4SS, we can use the global transient package as outer
    local transientPkg = FindFirstOf("Package")
    local rt = StaticConstructObject(rtClass, transientPkg, FName("OnlineRT_Slot" .. slot))

    if rt and rt:IsValid() then
        -- Configure the render target
        rt.SizeX = width
        rt.SizeY = height
        rt.RenderTargetFormat = 0  -- RTF_RGBA8 = 0
        rt.bAutoGenerateMips = false
        rt.ClearColor = { R = 0, G = 0, B = 0, A = 1 }

        -- Initialize the resource
        -- This creates the actual GPU texture
        rt:UpdateResourceImmediate()

        Logf("RenderTarget created: %s (%dx%d)", rt:GetFullName(), width, height)
        return rt
    else
        ErrLog("Failed to create RenderTarget2D for slot " .. slot)
        return nil
    end
end

-------------------------------------------------------------------------------
-- Scene Capture Setup
-- Spawns a SceneCaptureComponent2D and attaches it to the player's camera.
-- This component renders the player's POV to a TextureRenderTarget2D
-- at full resolution, completely independent of the viewport split.
-------------------------------------------------------------------------------

local function SetupSceneCaptureForPlayer(slot)
    local playerData = Players[slot]
    if not playerData then
        ErrLog("No player data for slot " .. slot)
        return false
    end

    local pawn = GetPlayerPawn(playerData.controller)
    if not pawn then
        ErrLog("No pawn for slot " .. slot)
        return false
    end

    local width = playerData.captureWidth
    local height = playerData.captureHeight

    -- Step 1: Create the render target
    local renderTarget = CreateRenderTarget(width, height, slot)
    if not renderTarget then
        ErrLog("Render target creation failed for slot " .. slot)
        -- Fall back: the C++ plugin can still try DXGI capture
        return false
    end
    playerData.renderTarget = renderTarget

    -- Step 2: Find or create SceneCaptureComponent2D
    -- We spawn a SceneCapture2D actor and attach it to the pawn
    Logf("Setting up SceneCapture2D for slot %d", slot)

    ExecuteInGameThread(function()
        -- Spawn a SceneCapture2D actor in the world
        local world = pawn:GetWorld()
        if not world or not world:IsValid() then
            ErrLog("Could not get world for scene capture")
            return
        end

        -- Get the camera manager for this player to find their active camera
        local cameraManager = playerData.controller.PlayerCameraManager
        if cameraManager and cameraManager:IsValid() then
            Logf("Slot %d camera manager: %s", slot, cameraManager:GetFullName())
        end

        -- Create SceneCapture2D actor via SpawnActor
        local captureClass = StaticFindObject("/Script/Engine.SceneCapture2D")
        if not captureClass then
            ErrLog("Could not find SceneCapture2D class")
            return
        end

        local location = pawn:K2_GetActorLocation()
        local rotation = pawn:K2_GetActorRotation()
        local transform = {
            Translation = location,
            Rotation = rotation,
            Scale3D = { X = 1, Y = 1, Z = 1 }
        }

        local captureActor = world:SpawnActor(captureClass, transform)
        if not captureActor or not captureActor:IsValid() then
            ErrLog("Failed to spawn SceneCapture2D actor for slot " .. slot)
            return
        end

        -- Configure the capture component
        local captureComp = captureActor.CaptureComponent2D
        if captureComp and captureComp:IsValid() then
            -- Assign our render target
            captureComp.TextureTarget = renderTarget

            -- Configure capture settings for quality + performance
            captureComp.bCaptureEveryFrame = true
            captureComp.bCaptureOnMovement = false
            captureComp.bAlwaysPersistRenderingState = true

            -- Match the player's FOV
            if cameraManager and cameraManager:IsValid() then
                captureComp.FOVAngle = cameraManager:GetFOVAngle()
            else
                captureComp.FOVAngle = 90.0
            end

            -- Capture scene settings
            captureComp.CaptureSource = 0  -- SCS_FinalColorLDR (what the player sees)

            -- Attach to the pawn so it follows the player
            captureActor:K2_AttachToActor(pawn, FName("None"), 0, 0, 0, false)

            playerData.sceneCapture = captureActor
            Logf("SceneCapture2D configured for slot %d: %s → %s",
                slot, captureActor:GetFullName(), renderTarget:GetFullName())
        else
            ErrLog("SceneCapture2D has no CaptureComponent2D")
            captureActor:K2_DestroyActor()
        end
    end)

    return true
end

-------------------------------------------------------------------------------
-- Per-frame: Sync SceneCapture position to player camera
-- The SceneCapture2D is attached to the pawn, but we need to match
-- the exact camera transform (the player camera manager applies offsets,
-- view shakes, etc. that the pawn transform doesn't include)
-------------------------------------------------------------------------------

local function UpdateSceneCaptureTransforms()
    for slot, data in pairs(Players) do
        if data.sceneCapture and data.sceneCapture:IsValid()
            and data.controller and data.controller:IsValid() then

            local camMgr = data.controller.PlayerCameraManager
            if camMgr and camMgr:IsValid() then
                local camLoc = camMgr:GetCameraLocation()
                local camRot = camMgr:GetCameraRotation()

                -- Update the scene capture actor to match the camera exactly
                data.sceneCapture:K2_SetActorLocationAndRotation(
                    camLoc, camRot, false, {}, true
                )

                -- Update FOV if it changed
                local captureComp = data.sceneCapture.CaptureComponent2D
                if captureComp and captureComp:IsValid() then
                    captureComp.FOVAngle = camMgr:GetFOVAngle()
                end
            end
        end
    end
end

-------------------------------------------------------------------------------
-- Player Viewport Override
-- Make the host player's viewport fullscreen (disable split-screen visually).
-- Remote players don't need viewports on screen — their SceneCapture2D
-- renders independently to render targets.
-------------------------------------------------------------------------------

local function OverrideHostViewportToFullscreen()
    if not HostPlayerController or not HostPlayerController:IsValid() then return end

    local localPlayer = HostPlayerController.Player
    if not localPlayer or not localPlayer:IsValid() then return end

    -- Force Player 1's viewport to fullscreen
    -- This prevents UE5 from splitting the screen when multiple local players exist
    --
    -- UGameViewportClient has GameLayerManager which controls viewport layout
    -- We set ForceDisableSplitscreen to keep Player 1 fullscreen
    local viewportClient = localPlayer.ViewportClient
    if viewportClient and viewportClient:IsValid() then
        -- Try the direct approach: set splitscreen info for the host player
        -- to occupy the full screen
        Logf("Overriding host viewport to fullscreen")

        -- UGameViewportClient::SetForceDisableSplitscreen is what we want
        -- It keeps Player 1 fullscreen while other players still exist in the world
        -- Their cameras just don't render to the screen — only to SceneCapture2D
        local gameStatics = GetGameplayStatics()
        if gameStatics then
            -- SetForceDisableSplitscreen(WorldContextObject, bDisable)
            gameStatics:SetForceDisableSplitscreen(GetWorldContextObject(), true)
            Log("Split-screen disabled — host renders fullscreen")
        end
    end
end

-------------------------------------------------------------------------------
-- Player Management
-------------------------------------------------------------------------------

local function SpawnRemotePlayer(slot, width, height)
    width = width or Config.DefaultCaptureWidth
    height = height or Config.DefaultCaptureHeight

    if slot < 2 or slot > Config.MaxRemotePlayers + 1 then
        ErrLog(string.format("Invalid slot %d (range: 2-%d)", slot, Config.MaxRemotePlayers + 1))
        return false
    end

    if Players[slot] then
        Logf("Slot %d already occupied", slot)
        return false
    end

    local controllers = CachePlayerControllers()
    local numExisting = 0
    for _ in pairs(controllers) do numExisting = numExisting + 1 end

    Logf("Spawning remote player slot %d at %dx%d (%d existing)", slot, width, height, numExisting)

    ExecuteInGameThread(function()
        local newController = GetGameplayStatics():CreatePlayer(
            controllers[1],
            numExisting,
            true
        )

        if newController and newController:IsValid() then
            Players[slot] = {
                controller = newController,
                pawn = GetPlayerPawn(newController),
                sceneCapture = nil,
                renderTarget = nil,
                captureWidth = width,
                captureHeight = height,
            }

            Logf("Player spawned slot %d: %s", slot, newController:GetFullName())

            -- Remap input for gamepad support
            RemapInputBindingsForGamepad()

            -- Override host viewport back to fullscreen
            -- (CreatePlayer enables splitscreen, we need to re-disable it)
            ExecuteWithDelay(100, function()
                OverrideHostViewportToFullscreen()
            end)

            -- Set up scene capture after a short delay (let pawn initialize)
            -- Team assignment is handled by the lobby — the Python session
            -- manager writes team_config.txt and we read it every tick.
            ExecuteWithDelay(Config.SceneCaptureSetupDelay or 500, function()
                -- Re-get pawn in case it wasn't ready yet
                Players[slot].pawn = GetPlayerPawn(newController)
                SetupSceneCaptureForPlayer(slot)

                -- Default to Team 1 (allies) until lobby config arrives
                if Players[slot].pawn then
                    TeamManager.SetPlayerTeam(newController, 1)
                    Logf("Slot %d defaulted to Team 1 (lobby will override)", slot)
                end
            end)
        else
            ErrLog("Failed to spawn player for slot " .. slot)
        end
    end)

    return true
end

local function RemoveRemotePlayer(slot)
    local data = Players[slot]
    if not data then return false end

    Logf("Removing player slot %d", slot)

    -- Destroy scene capture actor
    if data.sceneCapture and data.sceneCapture:IsValid() then
        data.sceneCapture:K2_DestroyActor()
    end

    -- Remove player
    if data.controller and data.controller:IsValid() then
        ExecuteInGameThread(function()
            GetGameplayStatics():RemovePlayer(data.controller, true)
        end)
    end

    Players[slot] = nil
    return true
end

local function RemoveAllRemotePlayers()
    for slot = Config.MaxRemotePlayers + 1, 2, -1 do
        RemoveRemotePlayer(slot)
    end
end

local function GetNextFreeSlot()
    for i = 2, Config.MaxRemotePlayers + 1 do
        if not Players[i] then return i end
    end
    return nil
end

local function GetPlayerCount()
    local count = 1 -- host
    for _ in pairs(Players) do count = count + 1 end
    return count
end

-------------------------------------------------------------------------------
-- Bridge: Lua mod ↔ Python host sidecar
-- Uses file-based IPC since UE4SS Lua has no socket support.
-- The C++ FrameExport plugin handles the actual frame data via shared memory.
-- This bridge is just for control messages (spawn/remove/config).
-------------------------------------------------------------------------------

local BRIDGE_DIR = nil

local function InitBridge()
    local appdata = os.getenv("LOCALAPPDATA")
    if appdata then
        BRIDGE_DIR = appdata .. "\\HalfSwordUE5\\Saved\\HalfSwordOnline"
        -- Directory creation is handled by the Python host sidecar
        -- (BRIDGE_DIR.mkdir(parents=True, exist_ok=True) in server.py)
        Logf("Bridge: %s", BRIDGE_DIR)
    else
        ErrLog("LOCALAPPDATA not found — bridge disabled")
    end
end

local function WriteBridgeState()
    if not BRIDGE_DIR then return end

    local f = io.open(BRIDGE_DIR .. "\\mod_state.txt", "w")
    if not f then return end

    f:write(string.format("player_count=%d\n", GetPlayerCount()))
    f:write(string.format("timestamp=%d\n", os.time()))
    f:write(string.format("initialized=%s\n", tostring(IsInitialized)))

    for slot, data in pairs(Players) do
        local alive = GetPlayerPawn(data.controller) ~= nil
        f:write(string.format("slot_%d_alive=%s\n", slot, tostring(alive)))
        f:write(string.format("slot_%d_width=%d\n", slot, data.captureWidth))
        f:write(string.format("slot_%d_height=%d\n", slot, data.captureHeight))
        f:write(string.format("slot_%d_has_capture=%s\n", slot,
            tostring(data.sceneCapture ~= nil and data.sceneCapture:IsValid())))
        if data.renderTarget and data.renderTarget:IsValid() then
            f:write(string.format("slot_%d_rt_name=%s\n", slot, data.renderTarget:GetFullName()))
        end
    end

    f:close()
end

local function ReadBridgeCommands()
    if not BRIDGE_DIR then return end

    local path = BRIDGE_DIR .. "\\host_commands.txt"
    local f = io.open(path, "r")
    if not f then return end

    local content = f:read("*a")
    f:close()
    os.remove(path)

    if not content or content == "" then return end

    for line in content:gmatch("[^\r\n]+") do
        local cmd = line:match("^(%S+)")
        if cmd == "spawn" then
            local w, h = line:match("spawn%s+(%d+)%s+(%d+)")
            w = tonumber(w) or Config.DefaultCaptureWidth
            h = tonumber(h) or Config.DefaultCaptureHeight
            local slot = GetNextFreeSlot()
            if slot then
                SpawnRemotePlayer(slot, w, h)
            else
                ErrLog("No free slots")
            end
        elseif cmd == "remove" then
            local slot = tonumber(line:match("remove%s+(%d+)"))
            if slot then RemoveRemotePlayer(slot) end
        elseif cmd == "remove_all" then
            RemoveAllRemotePlayers()
        elseif cmd == "resize" then
            local slot, w, h = line:match("resize%s+(%d+)%s+(%d+)%s+(%d+)")
            slot = tonumber(slot)
            w = tonumber(w)
            h = tonumber(h)
            if slot and w and h and Players[slot] then
                Logf("Resizing slot %d to %dx%d", slot, w, h)
                Players[slot].captureWidth = w
                Players[slot].captureHeight = h
                SetupSceneCaptureForPlayer(slot)
            end
        elseif cmd == "set_mode" then
            -- set_mode coop | pvp | teams | auto
            local mode = line:match("set_mode%s+(%S+)")
            if mode then
                Logf("Setting game mode: %s", mode)
                local allRemote = {}
                for s, d in pairs(Players) do
                    if d.controller then
                        table.insert(allRemote, d.controller)
                    end
                end
                TeamManager.AutoAssignForMode(mode, allRemote)
            end
        elseif cmd == "set_team" then
            -- set_team <slot> <team_int>
            local slot, team = line:match("set_team%s+(%d+)%s+(%d+)")
            slot = tonumber(slot)
            team = tonumber(team)
            if slot and team and Players[slot] then
                TeamManager.SetPlayerTeam(Players[slot].controller, team)
                Logf("Slot %d → Team %d", slot, team)
            end
        end
    end
end

-------------------------------------------------------------------------------
-- Team Config Reader
-- The Python session manager writes team assignments to team_config.txt
-------------------------------------------------------------------------------

local function ReadTeamConfig()
    if not BRIDGE_DIR then return end

    local path = BRIDGE_DIR .. "\\team_config.txt"
    local f = io.open(path, "r")
    if not f then return end

    local content = f:read("*a")
    f:close()

    if not content or content == "" then return end

    for line in content:gmatch("[^\r\n]+") do
        local slot, team = line:match("slot_(%d+)_team=(%d+)")
        slot = tonumber(slot)
        team = tonumber(team)
        if slot and team and Players[slot] and Players[slot].controller then
            TeamManager.SetPlayerTeam(Players[slot].controller, team)
        end
    end
end

-------------------------------------------------------------------------------
-- Tick
-------------------------------------------------------------------------------

local tickCounter = 0

local function OnTick()
    tickCounter = tickCounter + 1

    -- Every frame: sync scene capture transforms to player cameras
    UpdateSceneCaptureTransforms()

    -- Every ~30 frames: poll bridge, write state, read team config
    local interval = Config.BridgePollInterval or 30
    if tickCounter % interval == 0 then
        ReadBridgeCommands()
        ReadTeamConfig()
        WriteBridgeState()
    end
end

-------------------------------------------------------------------------------
-- Game Hooks
-------------------------------------------------------------------------------

local function OnClientRestart()
    Log("ClientRestart — new round")
    local controllers = CachePlayerControllers()
    if controllers[1] then
        HostPlayerController = controllers[1]
    end
    ExecuteWithDelay(1000, function()
        OverrideHostViewportToFullscreen()
    end)
end

local function OnCharacterConstructed(obj)
    Logf("Character constructed: %s", obj:GetFullName())
    RemapInputBindingsForGamepad()
end

-------------------------------------------------------------------------------
-- Init
-------------------------------------------------------------------------------

local function InitMod()
    Log("========================================")
    Log("  Half Sword Online Mod")
    Log("  N-Player Remote Play via SceneCapture")
    Log("========================================")

    -- Enable splitscreen (needed for CreatePlayer)
    local settings = GetGameMapsSettings()
    settings.bUseSplitscreen = true
    settings.bOffsetPlayerGamepadIds = Config.OffsetGamepad
    settings.TwoPlayerSplitscreenLayout = Config.SplitscreenLayout

    -- Cache host controller
    local controllers = CachePlayerControllers()
    if controllers[1] then
        HostPlayerController = controllers[1]
        Log("Host player controller cached")
    end

    -- Override viewport so host stays fullscreen
    ExecuteWithDelay(500, function()
        OverrideHostViewportToFullscreen()
    end)

    InitBridge()

    IsInitialized = true
    Log("Ready! Ctrl+N=add player, Ctrl+U=remove player")
end

-------------------------------------------------------------------------------
-- Entry Point
-------------------------------------------------------------------------------

InitMod()

NotifyOnNewObject("/Script/Engine.Character", OnCharacterConstructed)

RegisterHook("/Script/Engine.PlayerController:ClientRestart", OnClientRestart)

-- Tick: update scene captures every frame
RegisterHook("/Script/Engine.PlayerController:PlayerTick", function()
    OnTick()
end)

-- Keybinds
RegisterKeyBind(Key.N, { ModifierKey.CONTROL }, function()
    local slot = GetNextFreeSlot()
    if slot then
        SpawnRemotePlayer(slot)
    else
        ErrLog("All slots full!")
    end
end)

RegisterKeyBind(Key.U, { ModifierKey.CONTROL }, function()
    for i = Config.MaxRemotePlayers + 1, 2, -1 do
        if Players[i] then
            RemoveRemotePlayer(i)
            break
        end
    end
end)

RegisterKeyBind(Key.D, { ModifierKey.CONTROL }, DismissDeathScreen)

Log("Hooks registered. Mod active.")
