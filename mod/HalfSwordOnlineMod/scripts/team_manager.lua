--[[
    team_manager.lua
    Sets Team Int on player pawns based on what players chose in the lobby.

    Team system (Half Sword internal):
        0 = Deathmatch (attacks everyone)
        1 = Player/ally team (won't attack team 1)
        2 = Enemy team (NPCs/bosses, won't attack team 2)

    This module does NOT detect maps or game modes.
    Teams are entirely player-driven via the lobby UI.
    The Python session_manager writes team_config.txt which main.lua reads.

    Usage:
        local TeamManager = require("team_manager")
        TeamManager.SetPlayerTeam(playerController, 1)  -- allies
        TeamManager.SetPlayerTeam(playerController, 2)  -- enemies
        TeamManager.SetPlayerTeam(playerController, 0)  -- FFA
]]

local TeamManager = {}

-- ---------------------------------------------------------------------------
-- Logging
-- ---------------------------------------------------------------------------

local function log(msg)
    print("[TeamManager] " .. tostring(msg))
end

-- ---------------------------------------------------------------------------
-- Helpers
-- ---------------------------------------------------------------------------

local function getPawn(playerController)
    if not playerController then
        log("WARN: playerController is nil")
        return nil
    end
    if not playerController:IsValid() then
        log("WARN: playerController is invalid")
        return nil
    end
    local pawn = playerController.Pawn
    if not pawn or not pawn:IsValid() then
        log("WARN: no valid pawn")
        return nil
    end
    return pawn
end

-- ---------------------------------------------------------------------------
-- Core API
-- ---------------------------------------------------------------------------

--- Set the Team Int on a player's pawn.
-- @param playerController  The player controller whose pawn to modify
-- @param teamInt           0 (FFA), 1 (allies), or 2 (enemies)
-- @return boolean success
function TeamManager.SetPlayerTeam(playerController, teamInt)
    local pawn = getPawn(playerController)
    if not pawn then return false end

    local prev = pawn["Team Int"]
    pawn["Team Int"] = teamInt

    log(string.format("Team set: %d → %d (%s)",
        prev or -1, teamInt, tostring(playerController:GetFName())))
    return true
end

--- Get the current Team Int from a player's pawn.
-- @param playerController  The player controller to query
-- @return int or nil
function TeamManager.GetPlayerTeam(playerController)
    local pawn = getPawn(playerController)
    if not pawn then return nil end
    return pawn["Team Int"]
end

-- ---------------------------------------------------------------------------
-- Bulk operations (used by main.lua for convenience)
-- ---------------------------------------------------------------------------

--- Set all players to the same team.
-- @param controllers  Array of PlayerControllers
-- @param teamInt      Team number
function TeamManager.SetAllToTeam(controllers, teamInt)
    local count = 0
    for _, pc in ipairs(controllers) do
        if TeamManager.SetPlayerTeam(pc, teamInt) then
            count = count + 1
        end
    end
    log(string.format("Set %d players to Team %d", count, teamInt))
end

--- Apply team assignments from a table.
-- @param assignments  Table of { [playerController] = teamInt }
function TeamManager.ApplyAssignments(assignments)
    local count = 0
    for pc, teamInt in pairs(assignments) do
        if TeamManager.SetPlayerTeam(pc, teamInt) then
            count = count + 1
        end
    end
    log(string.format("Applied %d team assignments", count))
end

return TeamManager
