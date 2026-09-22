from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path
import json

from game.game import Game
from server.rooms import RoomManager

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="Card Game")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

rooms = RoomManager()


@app.get("/", response_class=HTMLResponse)
async def index():
    return (BASE_DIR / "templates" / "index.html").read_text(encoding="utf-8")


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    room = None
    player_id = None

    try:
        while True:
            raw = await websocket.receive_text()
            message = json.loads(raw)
            action = message.get("action")

            if action == "create_room":
                name = str(message.get("name", "Player")).strip()[:24] or "Player"
                bot_count = max(1, min(5, int(message.get("bot_count", 1))))
                room, player_id = rooms.create_room(name, bot_count)
                room.connections[player_id] = websocket
                await room.send_state()

            elif action == "join_room":
                code = str(message.get("room", "")).strip().upper()
                name = str(message.get("name", "Player")).strip()[:24] or "Player"
                room = rooms.get(code)
                if room is None:
                    await websocket.send_json({"type": "error", "message": "Raum nicht gefunden."})
                    continue
                result = room.add_human(name)
                if result is None:
                    await websocket.send_json({"type": "error", "message": "Raum ist voll oder das Spiel läuft bereits."})
                    continue
                player_id = result
                room.connections[player_id] = websocket
                await room.send_state()

            elif action == "start_game":
                if room is None or player_id is None:
                    continue
                result = room.game.start(player_id)
                if result:
                    await room.run_until_human_turn()
                await room.send_state()

            elif action == "choose_trump":
                if room and player_id:
                    result = room.game.choose_trump(player_id, message.get("color"))
                    if result["ok"]:
                        await room.run_until_human_turn()
                    await room.send_state()

            elif action == "bid":
                if room and player_id:
                    result = room.game.submit_bid(player_id, int(message.get("bid", -1)))
                    if result["ok"]:
                        await room.run_until_human_turn()
                    await room.send_state()

            elif action == "play_card":
                if room and player_id:
                    result = room.game.play_card(player_id, message.get("card_id"))
                    if result["ok"]:
                        await room.run_until_human_turn()
                    await room.send_state()

    except WebSocketDisconnect:
        if room and player_id:
            room.connections.pop(player_id, None)
