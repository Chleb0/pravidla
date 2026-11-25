## NOTE: THIS REPOSITORY CODE IS FULLY AI-GENERATED

---

# Event game control

A live team game: players walk up to any of the devices, pick their team, press a command, and see what it did to their team's inventory.
Each command cycles through a list of effects, and each team keeps its own position in those cycles.

## Running it

On **one** computer (the server):
```
python3 server.py              # Windows: python server.py
python3 server.py --pin 1234   # recommended: protect the admin page
python3 server.py 8080         # use another port
```
You only need Python 3. There is nothing to install. The server prints three addresses:

| Page | Address | Who uses it |
|---|---|---|
| Player devices | `http://<ip>:8000` | The 6 devices players use: team → command → result |
| Scoreboard | `http://<ip>:8000/?board` | A projector or TV. Read-only view of every inventory |
| Admin | `http://<ip>:8000/?admin` | Game master: fix inventories, undo, reset, edit rules live |

The game is saved to `game_state.json` after every action. If you restart the server, the game continues where it left off.

## Rules: `game_config.json`

```jsonc
{
  "title": "Event Game",
  "rotation": "per_command",     // or "team_wide" (see below)
  "allow_negative": false,       // false = a command is refused if the team can't afford it
  "show_next_effect": false,     // true = players see what a command will do before pressing it
  "return_after_seconds": 15,    // an idle device goes back to the team picker (0 = never)
  "teams": [{"name": "Red", "color": "#e03131"}, ...],
  "items": [{"id": "wood", "name": "Wood", "icon": "🪵"}, ...],
  "start_inventory": {"wood": 0, "food": 2},
  "commands": [
    {"id": "forest", "name": "Forest", "effects": [
      {"text": "You chop a few trees.", "change": {"wood": 2}},
      {"text": "A big oak falls!",      "change": {"wood": 4, "food": -1}}
    ]}
  ]
}
```

**Rotation modes**
- `per_command`: each time a team uses a command, that command moves to its next effect for that team only. After the last effect it starts over at the first.
- `team_wide`: each action a team takes moves *all* of that team's commands forward one step.

A refused action (the team couldn't afford it) does not rotate anything.

You can change the rules in two ways:
- Edit the file and restart the server.
- Edit the rules on the admin page and click **Save rules**. The new rules reach every device immediately and inventories are kept.

Invalid rules (bad JSON, unknown items, and so on) are rejected with an error message, and the old rules stay in place.

## Troubleshooting

- **The other devices can't connect:** allow Python through the server computer's firewall. Windows shows a prompt the first time it runs; choose "Private networks". Also check that every device is on the same Wi-Fi network, not a guest network.
- **Something went wrong in the game:** use **Undo** on the admin page. It reverses the most recent change, including rule edits.

## Extending

Mechanics that go beyond "add or remove items", such as one team's action affecting another team, go in `server.py` as a new `act_*` method registered in `PUBLIC`.
