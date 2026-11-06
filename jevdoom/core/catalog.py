"""Game catalog and observation labels."""

EVALUATOR_FIELDS = (
    "hits",
    "damage_dealt",
    "kills",
    "items",
    "secrets",
    "damage_taken",
    "hits_taken",
)
BUTTONS = [
    "MOVE_FORWARD_BACKWARD_DELTA",
    "MOVE_LEFT_RIGHT_DELTA",
    "TURN_LEFT_RIGHT_DELTA",
    "ATTACK",
    "USE",
    "SELECT_WEAPON2",
    "SELECT_WEAPON3",
    "SELECT_WEAPON4",
    "SELECT_WEAPON5",
    "SELECT_WEAPON6",
    "SELECT_WEAPON7",
]
MONSTERS = {
    "DoomImp",
    "Zombieman",
    "ShotgunGuy",
    "ChaingunGuy",
    "Demon",
    "Spectre",
    "Cacodemon",
    "BaronOfHell",
    "HellKnight",
    "LostSoul",
    "Revenant",
    "Fatso",
    "Arachnotron",
    "Archvile",
    "PainElemental",
    "Cyberdemon",
    "SpiderMastermind",
    "WolfensteinSS",
}
HEALTH = {"Stimpack", "Medikit", "HealthBonus", "Soulsphere", "Megasphere", "Berserk"}
ARMOR = {"ArmorBonus", "GreenArmor", "BlueArmor"}
AMMO = {
    "Clip",
    "ClipBox",
    "Shell",
    "ShellBox",
    "RocketAmmo",
    "RocketBox",
    "Cell",
    "CellPack",
    "Backpack",
}
WEAPONS = {
    "Shotgun",
    "SuperShotgun",
    "Chaingun",
    "RocketLauncher",
    "PlasmaRifle",
    "BFG9000",
    "Chainsaw",
}
KEYS = {"BlueCard", "RedCard", "YellowCard", "BlueSkull", "RedSkull", "YellowSkull"}
WEAPON_NAMES = {
    1: "fist or chainsaw",
    2: "pistol",
    3: "shotgun or super shotgun",
    4: "chaingun",
    5: "rocket launcher",
    6: "plasma rifle",
    7: "BFG",
}
PICKUP_WEAPON_SLOTS = {
    "Shotgun": 3,
    "SuperShotgun": 3,
    "Chaingun": 4,
    "RocketLauncher": 5,
    "PlasmaRifle": 6,
    "BFG9000": 7,
    "Chainsaw": 1,
}


def owns_pickup(name, carrying):
    slot = PICKUP_WEAPON_SLOTS.get(name)
    weapon = next((item for item in carrying if item["slot"] == slot), None)
    if weapon is None:
        return False
    if name in ("Chainsaw", "Shotgun", "SuperShotgun"):
        return weapon.get("count", 1) >= 2
    return True


def category(name):
    if name in MONSTERS:
        return "enemy"
    if name == "ExplosiveBarrel":
        return "obstacle"
    if name in HEALTH:
        return "health"
    if name in ARMOR:
        return "armor"
    if name in AMMO:
        return "ammo"
    if name in WEAPONS:
        return "weapon"
    if name in KEYS:
        return "key"
    if any(x in name for x in ("Ball", "Rocket", "Plasma")):
        return "projectile"
    return None


VARIABLES = (
    "POSITION_X",
    "POSITION_Y",
    "POSITION_Z",
    "ANGLE",
    "HEALTH",
    "ARMOR",
    "SELECTED_WEAPON",
    "SELECTED_WEAPON_AMMO",
    "KILLCOUNT",
    "ITEMCOUNT",
    "SECRETCOUNT",
    "HITCOUNT",
    "DAMAGECOUNT",
    "DAMAGE_TAKEN",
    "HITS_TAKEN",
    "VELOCITY_X",
    "VELOCITY_Y",
    *(f"AMMO{i}" for i in WEAPON_NAMES),
    *(f"WEAPON{i}" for i in WEAPON_NAMES),
)
