from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
import random
import string

app = FastAPI(title="Card Game")


# ============================================================
# GAME DATA
# ============================================================

class CardType(str, Enum):
    NUMBER = "number"
    WIZARD = "wizard"
    FOOL = "fool"


class Color(str, Enum):
    GREEN = "green"
    BLUE = "blue"
    YELLOW = "yellow"
    RED = "red"


COLORS = [Color.GREEN, Color.BLUE, Color.YELLOW, Color.RED]


@dataclass(frozen=True)
class Card:
    id: str
    type: CardType
    value: Optional[int] = None
    color: Optional[Color] = None
    ui_color: Optional[Color] = None

    def public(self):
        return {
            "id": self.id,
            "type": self.type.value,
            "value": self.value,
            "color": self.color.value if self.color else None,
            "ui_color": self.ui_color.value if self.ui_color else None,
        }


def create_deck():
    deck = []

    for color in COLORS:
        for value in range(1, 14):
            deck.append(Card(
                id=f"{color.value}-{value}",
                type=CardType.NUMBER,
                value=value,
                color=color,
                ui_color=color,
            ))

    # Four Wizards. Their UI colors are cosmetic only.
    for i, color in enumerate(COLORS, 1):
        deck.append(Card(
            id=f"wizard-{i}",
            type=CardType.WIZARD,
            ui_color=color,
        ))

    # Four Fools. Their UI colors are cosmetic only.
    for i, color in enumerate(COLORS, 1):
        deck.append(Card(
            id=f"fool-{i}",
            type=CardType.FOOL,
            ui_color=color,
        ))

    assert len(deck) == 60
    return deck


@dataclass
class Player:
    id: str
    name: str
    is_bot: bool = False
    hand: list[Card] = field(default_factory=list)
    bid: Optional[int] = None
    tricks_won: int = 0
    score: int = 0
    round_scores: list[int] = field(default_factory=list)


@dataclass
class PlayedCard:
    player_id: str
    card: Card


# ============================================================
# RULES
# ============================================================

def get_legal_cards(hand, trick, trump_color):
    if not trick:
        return list(hand)

    # Wizards and Fools may always be played.
    specials = [
        c for c in hand
        if c.type in (CardType.WIZARD, CardType.FOOL)
    ]

    # The lead color is established by the first NUMBER card.
    lead_color = next(
        (
            p.card.color
            for p in trick
            if p.card.type == CardType.NUMBER
        ),
        None,
    )

    # If the trick currently contains only Wizards/Fools,
    # any card may be played.
    if lead_color is None:
        return list(hand)

    suited = [
        c for c in hand
        if c.type == CardType.NUMBER
        and c.color == lead_color
    ]

    # If the player can follow suit, they must do so.
    # Wizards/Fools remain exceptions.
    if suited:
        return suited + specials

    # If they cannot follow suit, any card is legal.
    return list(hand)


def determine_winner(trick, trump_color):
    if not trick:
        return None

    # First Wizard wins, regardless of anything else.
    for played in trick:
        if played.card.type == CardType.WIZARD:
            return played.player_id

    # If the entire trick consists of Fools,
    # the player who started the trick wins.
    if all(p.card.type == CardType.FOOL for p in trick):
        return trick[0].player_id

    numbered = [
        p for p in trick
        if p.card.type == CardType.NUMBER
    ]

    lead_color = next(
        (p.card.color for p in numbered),
        None,
    )

    # Highest Trump wins.
    if trump_color is not None:
        trumps = [
            p for p in numbered
            if p.card.color == trump_color
        ]
        if trumps:
            return max(
                trumps,
                key=lambda p: p.card.value
            ).player_id

    # Otherwise highest card of the lead color wins.
    leads = [
        p for p in numbered
        if p.card.color == lead_color
    ]
    if leads:
        return max(
            leads,
            key=lambda p: p.card.value
        ).player_id

    # Defensive fallback.
    return trick[0].player_id


def calculate_round_score(bid, tricks_won):
    if bid == tricks_won:
        return 20 + tricks_won * 10
    return -abs(bid - tricks_won) * 10


# ============================================================
# BOT
# ============================================================

class SimpleBot:

    def choose_bid(self, game, player):
        estimate = 0

        for card in player.hand:
            if card.type == CardType.WIZARD:
                estimate += 1
            elif card.type == CardType.NUMBER:
                if card.value >= 12:
                    estimate += 0.55
                elif card.value >= 9:
                    estimate += 0.25

        return max(
            0,
            min(game.cards_per_player, round(estimate))
        )

    def choose_card(self, game, player):
        legal = game.legal_cards_for(player.id)

        if not legal:
            return None

        target = player.bid or 0

        # If the bot still needs tricks, prefer Wizard.
        if player.tricks_won < target:
            wizards = [
                c for c in legal
                if c.type == CardType.WIZARD
            ]
            if wizards:
                return wizards[0].id

        # If target has already been reached, prefer Fool.
        if player.tricks_won >= target:
            fools = [
                c for c in legal
                if c.type == CardType.FOOL
            ]
            if fools:
                return fools[0].id

        # Otherwise play the lowest legal numbered card.
        numbers = [
            c for c in legal
            if c.type == CardType.NUMBER
        ]
        if numbers:
            return min(numbers, key=lambda c: c.value).id

        return legal[0].id

    def choose_trump(self, player):
        counts = {}

        for card in player.hand:
            if card.type == CardType.NUMBER:
                counts[card.color] = counts.get(card.color, 0) + 1

        if counts:
            return max(
                counts,
                key=counts.get
            ).value

        return "red"


# ============================================================
# GAME ENGINE
# ============================================================

class Game:

    def __init__(self, game_id, players):
        self.id = game_id
        self.players = players

        self.round_number = 0
        self.starting_player_index = 0
        self.current_player_index = 0

        self.phase = "lobby"

        self.deck = []

        self.trump_card = None
        self.trump_color = None
        self.trump_reveal = None

        self.current_trick = []
        self.trick_history = []

        self.message = "Warte auf Spieler."

        self.bot = SimpleBot()

    @property
    def cards_per_player(self):
        return self.round_number

    @property
    def current_player(self):
        return self.players[self.current_player_index]

    @property
    def starting_player(self):
        return self.players[self.starting_player_index]

    @property
    def max_rounds(self):
        return 60 // len(self.players)

    def start(self, requester_id):
        if requester_id != self.players[0].id:
            return False

        if len(self.players) < 3:
            return False

        if not any(p.is_bot for p in self.players):
            return False

        return self.start_next_round()

    def start_next_round(self):

        self.round_number += 1

        if self.round_number > self.max_rounds:
            self.phase = "game_over"
            self.message = "Spiel beendet."
            return True

        # Every round starts with a completely fresh 60-card deck.
        self.deck = create_deck()
        random.shuffle(self.deck)

        for player in self.players:
            player.hand.clear()
            player.bid = None
            player.tricks_won = 0

        # Deal clockwise, beginning with this round's start player.
        for _ in range(self.cards_per_player):
            for offset in range(len(self.players)):
                index = (
                    self.starting_player_index + offset
                ) % len(self.players)

                self.players[index].hand.append(
                    self.deck.pop()
                )

        # Reveal one card from the remaining deck.
        self.trump_card = self.deck.pop()

        self.trump_color = None
        self.trump_reveal = self.trump_card.type.value

        self.current_trick = []
        self.trick_history = []

        self.current_player_index = (
            self.starting_player_index
        )

        if self.trump_card.type == CardType.NUMBER:

            self.trump_color = self.trump_card.color

            self.phase = "bidding"

            self.message = (
                f"Runde {self.round_number}: "
                "Ansagen."
            )

        elif self.trump_card.type == CardType.FOOL:

            # Fool revealed = no trump this round.
            self.trump_color = None
            self.phase = "bidding"

            self.message = (
                "Narr aufgedeckt: "
                "Diese Runde gibt es keinen Trumpf."
            )

        else:

            # Wizard revealed = starting player chooses trump.
            self.phase = "choose_trump"

            self.message = (
                "Zauberer aufgedeckt: "
                "Der Startspieler wählt den Trumpf."
            )

        return True

    def choose_trump(self, player_id, color):

        if (
            self.phase != "choose_trump"
            or player_id != self.current_player.id
        ):
            return {
                "ok": False,
                "message": "Du bist nicht an der Reihe."
            }

        try:
            self.trump_color = Color(color)
        except ValueError:
            return {
                "ok": False,
                "message": "Ungültige Trumpffarbe."
            }

        self.phase = "bidding"

        self.current_player_index = (
            self.starting_player_index
        )

        self.message = (
            f"Trumpf: {self.trump_color.value}."
        )

        return {"ok": True}

    def submit_bid(self, player_id, bid):

        if (
            self.phase != "bidding"
            or player_id != self.current_player.id
        ):
            return {
                "ok": False,
                "message": "Du bist nicht an der Reihe."
            }

        if not 0 <= bid <= self.cards_per_player:
            return {
                "ok": False,
                "message": "Ungültige Ansage."
            }

        self.current_player.bid = bid

        if all(
            p.bid is not None
            for p in self.players
        ):
            self.phase = "playing"

            self.current_player_index = (
                self.starting_player_index
            )

            self.message = (
                "Alle Ansagen sind abgegeben. "
                "Der erste Stich beginnt."
            )

        else:
            self.current_player_index = (
                self.current_player_index + 1
            ) % len(self.players)

            self.message = (
                f"{self.current_player.name} "
                f"hat {bid} angesagt."
            )

        return {"ok": True}

    def legal_cards_for(self, player_id):

        player = next(
            (
                p for p in self.players
                if p.id == player_id
            ),
            None
        )

        if not player:
            return []

        return get_legal_cards(
            player.hand,
            self.current_trick,
            self.trump_color
        )

    def play_card(self, player_id, card_id):

        if (
            self.phase != "playing"
            or player_id != self.current_player.id
        ):
            return {
                "ok": False,
                "message": "Du bist nicht an der Reihe."
            }

        player = self.current_player

        card = next(
            (
                c for c in player.hand
                if c.id == card_id
            ),
            None
        )

        if card is None:
            return {
                "ok": False,
                "message": "Karte nicht gefunden."
            }

        legal = self.legal_cards_for(player_id)

        if card not in legal:
            return {
                "ok": False,
                "message": "Diese Karte darf nicht gespielt werden."
            }

        player.hand.remove(card)

        self.current_trick.append(
            PlayedCard(
                player_id=player_id,
                card=card
            )
        )

        # Stich not complete yet.
        if len(self.current_trick) < len(self.players):

            self.current_player_index = (
                self.current_player_index + 1
            ) % len(self.players)

            self.message = (
                f"{player.name} spielt eine Karte."
            )

            return {"ok": True}

        # Resolve completed trick.
        winner_id = determine_winner(
            self.current_trick,
            self.trump_color
        )

        winner = next(
            p for p in self.players
            if p.id == winner_id
        )

        winner.tricks_won += 1

        self.trick_history.append([
            {
                "player_id": p.player_id,
                "card": p.card.public()
            }
            for p in self.current_trick
        ])

        self.current_trick = []

        # Round complete.
        if all(
            len(p.hand) == 0
            for p in self.players
        ):
            self.finish_round()

        else:
            # Winner leads next trick.
            self.current_player_index = (
                self.players.index(winner)
            )

            self.message = (
                f"{winner.name} gewinnt den Stich."
            )

        return {"ok": True}

    def finish_round(self):

        for player in self.players:

            points = calculate_round_score(
                player.bid,
                player.tricks_won
            )

            player.score += points
            player.round_scores.append(points)

        if self.round_number >= self.max_rounds:

            self.phase = "game_over"

            self.message = (
                "Alle Runden gespielt. "
                "Spiel beendet."
            )

        else:

            self.phase = "round_result"

            self.message = (
                "Runde beendet."
            )

    def continue_after_round(self):

        if self.phase != "round_result":
            return

        # Starting player rotates clockwise.
        self.starting_player_index = (
            self.starting_player_index + 1
        ) % len(self.players)

        self.start_next_round()

    def public_state(self, viewer_id=None):

        viewer = next(
            (
                p for p in self.players
                if p.id == viewer_id
            ),
            None
        )

        return {
            "type": "state",

            "phase": self.phase,

            "round": self.round_number,

            "max_rounds": self.max_rounds,

            "cards_per_player":
                self.cards_per_player,

            "starting_player":
                self.starting_player.id,

            "current_player":
                self.current_player.id
                if self.players else None,

            "trump_card":
                self.trump_card.public()
                if self.trump_card else None,

            "trump_color":
                self.trump_color.value
                if self.trump_color else None,

            "trump_reveal":
                self.trump_reveal,

            "message":
                self.message,

            "players": [
                {
                    "id": p.id,
                    "name": p.name,
                    "is_bot": p.is_bot,
                    "card_count": len(p.hand),
                    "bid": p.bid,
                    "tricks_won": p.tricks_won,
                    "score": p.score,
                    "round_scores": p.round_scores,
                }
                for p in self.players
            ],

            "your_hand":
                [
                    c.public()
                    for c in viewer.hand
                ]
                if viewer else [],

            "legal_cards":
                [
                    c.id
                    for c in self.legal_cards_for(
                        viewer_id
                    )
                ]
                if viewer else [],

            "current_trick": [
                {
                    "player_id": p.player_id,
                    "card": p.card.public()
                }
                for p in self.current_trick
            ],
        }


# ============================================================
# ROOMS
# ============================================================

class Room:

    def __init__(self, code, owner_name, bot_count):

        self.code = code

        self.connections = {}

        players = [
            Player(
                id="p1",
                name=owner_name,
                is_bot=False
            )
        ]

        for i in range(bot_count):
            players.append(
                Player(
                    id=f"bot-{i + 1}",
                    name=f"Bot {i + 1}",
                    is_bot=True
                )
            )

        self.game = Game(
            code,
            players
        )

    def add_human(self, name):

        if self.game.phase != "lobby":
            return None

        if len(self.game.players) >= 6:
            return None

        human_count = sum(
            not p.is_bot
            for p in self.game.players
        )

        player_id = f"p{human_count + 1}"

        self.game.players.append(
            Player(
                id=player_id,
                name=name,
                is_bot=False
            )
        )

        return player_id

    async def send_state(self):

        dead = []

        for player_id, websocket in list(
            self.connections.items()
        ):

            try:
                await websocket.send_json(
                    self.game.public_state(player_id)
                )

            except Exception:
                dead.append(player_id)

        for player_id in dead:
            self.connections.pop(
                player_id,
                None
            )

    async def run_bots(self):

        while True:

            if self.game.phase == "round_result":
                return

            if self.game.phase in (
                "game_over",
                "lobby"
            ):
                return

            current = self.game.current_player

            if not current.is_bot:
                return

            if self.game.phase == "choose_trump":

                color = self.game.bot.choose_trump(
                    current
                )

                self.game.choose_trump(
                    current.id,
                    color
                )

            elif self.game.phase == "bidding":

                bid = self.game.bot.choose_bid(
                    self.game,
                    current
                )

                self.game.submit_bid(
                    current.id,
                    bid
                )

            elif self.game.phase == "playing":

                card_id = self.game.bot.choose_card(
                    self.game,
                    current
                )

                if card_id is None:
                    return

                self.game.play_card(
                    current.id,
                    card_id
                )

            else:
                return

            await self.send_state()


class RoomManager:

    def __init__(self):
        self.rooms = {}

    def create_room(
        self,
        owner_name,
        bot_count
    ):

        while True:

            code = "".join(
                random.choice(
                    string.ascii_uppercase
                    + string.digits
                )
                for _ in range(5)
            )

            if code not in self.rooms:
                break

        room = Room(
            code,
            owner_name,
            bot_count
        )

        self.rooms[code] = room

        return room, "p1"

    def get(self, code):
        return self.rooms.get(
            code.upper()
        )


rooms = RoomManager()


# ============================================================
# HTTP
# ============================================================

HTML = r"""
<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport"
      content="width=device-width,initial-scale=1">
<title>Card Game</title>

<style>
:root {
  --bg:#140f19;
  --panel:#211826;
  --panel2:#2b2031;
  --text:#f7eef8;
  --muted:#bcaebe;
  --accent:#e7a9d8;
  --green:#42b883;
  --blue:#5595e8;
  --yellow:#e4bb4e;
  --red:#e26b72;
}

* {
  box-sizing:border-box;
}

body {
  margin:0;
  min-height:100vh;
  background:
    radial-gradient(
      circle at top,
      #2a1930,
      var(--bg) 55%
    );
  color:var(--text);
  font-family:
    Inter,
    system-ui,
    sans-serif;
}

main {
  max-width:980px;
  margin:auto;
  padding:16px;
}

.panel {
  background:
    rgba(33,24,38,.94);
  border:
    1px solid rgba(255,255,255,.08);
  border-radius:18px;
  padding:18px;
  margin-bottom:14px;
  box-shadow:
    0 10px 35px rgba(0,0,0,.18);
}

.hidden {
  display:none !important;
}

input,
select,
button {
  width:100%;
  padding:13px;
  margin:6px 0;
  border-radius:12px;
  border:
    1px solid rgba(255,255,255,.12);
  background:var(--panel2);
  color:var(--text);
  font:inherit;
}

button {
  background:var(--accent);
  color:#201320;
  font-weight:700;
  cursor:pointer;
}

.buttons {
  display:grid;
  grid-template-columns:1fr 1fr;
  gap:8px;
}

.topbar {
  display:flex;
  justify-content:space-between;
  gap:10px;
  padding:8px 2px 14px;
}

.players {
  display:grid;
  grid-template-columns:
    repeat(auto-fit,minmax(150px,1fr));
  gap:10px;
}

.player {
  background:var(--panel);
  padding:12px;
  border-radius:14px;
  border:
    1px solid rgba(255,255,255,.07);
}

.player.current {
  outline:2px solid var(--accent);
}

.cards,
.trick {
  display:flex;
  flex-wrap:wrap;
  gap:10px;
}

.card {
  width:74px;
  height:104px;
  border-radius:12px;
  background:#faf7f3;
  color:#151015;
  display:flex;
  flex-direction:column;
  align-items:center;
  justify-content:center;
  font-weight:800;
  font-size:1.4rem;
  box-shadow:
    0 6px 14px rgba(0,0,0,.25);
  border:3px solid transparent;
  transition:.15s;
}

.card.small {
  width:62px;
  height:82px;
}

.card.legal {
  cursor:pointer;
  transform:translateY(-5px);
  box-shadow:
    0 12px 22px rgba(0,0,0,.35);
}

.card.faded {
  opacity:.35;
}

.card.green {
  border-color:var(--green);
}

.card.blue {
  border-color:var(--blue);
}

.card.yellow {
  border-color:var(--yellow);
}

.card.red {
  border-color:var(--red);
}

.trick-card {
  display:flex;
  flex-direction:column;
  align-items:center;
  gap:4px;
  font-size:.75rem;
}

#controls {
  display:flex;
  flex-wrap:wrap;
  gap:8px;
  align-items:center;
}

.bid-grid,
.trump-grid {
  display:flex;
  gap:8px;
  flex-wrap:wrap;
}

.bid-grid button,
.trump-grid button {
  width:auto;
  min-width:52px;
}

.score-table {
  width:100%;
  border-collapse:collapse;
}

.score-table th,
.score-table td {
  padding:8px;
  border-bottom:
    1px solid rgba(255,255,255,.08);
  text-align:left;
}

@media(max-width:600px) {
  main {
    padding:10px;
  }

  .card {
    width:60px;
    height:88px;
  }

  .buttons {
    grid-template-columns:1fr;
  }
}
</style>
</head>

<body>

<main>

<section id="lobby" class="panel">

<h1>🃏 Card Game</h1>

<p>
60 Karten · 3–6 Spieler · Multiplayer
</p>

<input
  id="name"
  maxlength="24"
  placeholder="Dein Name"
  value="Spieler">

<input
  id="room"
  maxlength="5"
  placeholder="Raumcode zum Beitreten">

<select id="bots">
  <option value="1">1 Bot</option>
  <option value="2">2 Bots</option>
  <option value="3">3 Bots</option>
  <option value="4">4 Bots</option>
  <option value="5">5 Bots</option>
</select>

<div class="buttons">

<button onclick="createRoom()">
Raum erstellen
</button>

<button onclick="joinRoom()">
Raum beitreten
</button>

</div>

<p id="lobbyMsg"></p>

</section>


<section id="game" class="hidden">

<header class="topbar">

<div>
<strong>🃏 Card Game</strong>
<span id="roomLabel"></span>
</div>

<div id="roundLabel"></div>

</header>


<section class="status panel">

<div id="message"></div>

<div id="trump"></div>

</section>


<section
  id="players"
  class="players">
</section>


<section class="table panel">

<h2>Stich</h2>

<div
  id="trick"
  class="trick">
</div>

</section>


<section
  id="controls"
  class="panel">
</section>


<section class="hand panel">

<h2>Deine Karten</h2>

<div
  id="hand"
  class="cards">
</div>

</section>

</section>

</main>


<script>

let ws = null;
let myId = null;
let state = null;
let roomCode = null;


function connect() {

  const proto =
    location.protocol === "https:"
      ? "wss"
      : "ws";

  ws = new WebSocket(
    `${proto}://${location.host}/ws`
  );

  ws.onmessage = event => {

    const msg =
      JSON.parse(event.data);

    if (msg.type === "identity") {

      myId = msg.player_id;
      roomCode = msg.room;

      document.getElementById(
        "roomLabel"
      ).textContent =
        " · " + roomCode;

      return;
    }

    if (msg.type === "error") {

      document.getElementById(
        "lobbyMsg"
      ).textContent =
        msg.message;

      return;
    }

    state = msg;

    render();
  };

  ws.onclose = () => {

    setTimeout(
      connect,
      1500
    );
  };
}


function send(action, data = {}) {

  if (
    ws &&
    ws.readyState === WebSocket.OPEN
  ) {

    ws.send(
      JSON.stringify({
        action,
        ...data
      })
    );
  }
}


function createRoom() {

  send(
    "create_room",
    {
      name:
        document.getElementById(
          "name"
        ).value,

      bot_count:
        Number(
          document.getElementById(
            "bots"
          ).value
        )
    }
  );
}


function joinRoom() {

  send(
    "join_room",
    {
      name:
        document.getElementById(
          "name"
        ).value,

      room:
        document.getElementById(
          "room"
        ).value
    }
  );
}


function startGame() {
  send("start_game");
}


function nextRound() {
  send("next_round");
}


function bid(value) {

  send(
    "bid",
    {
      bid:value
    }
  );
}


function chooseTrump(color) {

  send(
    "choose_trump",
    {
      color
    }
  );
}


function play(cardId) {

  send(
    "play_card",
    {
      card_id:cardId
    }
  );
}


function render() {

  if (!state)
    return;

  document
    .getElementById("lobby")
    .classList
    .add("hidden");

  document
    .getElementById("game")
    .classList
    .remove("hidden");


  document
    .getElementById("roundLabel")
    .textContent =
      state.round
        ? `Runde ${state.round} / ${state.max_rounds}`
        : "";


  document
    .getElementById("message")
    .textContent =
      state.message || "";


  let trumpText = "";

  if (state.trump_color) {

    trumpText =
      "Trumpf: "
      + symbol(state.trump_color);

  } else if (
    state.trump_reveal === "fool"
  ) {

    trumpText =
      "Trumpf: keiner";

  } else if (
    state.phase === "choose_trump"
  ) {

    trumpText =
      "Trumpf: Startspieler muss wählen";
  }


  document
    .getElementById("trump")
    .textContent =
      trumpText;


  renderPlayers();
  renderTrick();
  renderControls();
  renderHand();
}


function renderPlayers() {

  const el =
    document.getElementById(
      "players"
    );

  el.innerHTML = "";


  state.players.forEach(
    player => {

      const div =
        document.createElement(
          "div"
        );

      div.className =
        "player"
        + (
          player.id ===
          state.current_player
            ? " current"
            : ""
        );


      div.innerHTML = `
        <strong>
          ${escapeHtml(player.name)}
        </strong>
        ${player.is_bot ? "🤖" : "👤"}
        <br>
        Karten: ${player.card_count}
        <br>
        Ansage: ${player.bid ?? "—"}
        <br>
        Stiche: ${player.tricks_won}
        <br>
        Punkte: ${player.score}
      `;


      el.appendChild(div);
    }
  );
}


function renderTrick() {

  const el =
    document.getElementById(
      "trick"
    );

  el.innerHTML = "";


  state.current_trick.forEach(
    item => {

      const player =
        state.players.find(
          p =>
            p.id ===
            item.player_id
        );


      const wrapper =
        document.createElement(
          "div"
        );

      wrapper.className =
        "trick-card";


      wrapper.appendChild(
        makeCard(
          item.card,
          true
        )
      );


      const label =
        document.createElement(
          "span"
        );

      label.textContent =
        player
          ? player.name
          : item.player_id;


      wrapper.appendChild(label);

      el.appendChild(wrapper);
    }
  );
}


function renderHand() {

  const el =
    document.getElementById(
      "hand"
    );

  el.innerHTML = "";


  state.your_hand.forEach(
    card => {

      const element =
        makeCard(
          card,
          false
        );


      const legal =
        state.legal_cards.includes(
          card.id
        );


      if (
        legal &&
        state.current_player === myId &&
        state.phase === "playing"
      ) {

        element.classList.add(
          "legal"
        );

        element.onclick =
          () => play(card.id);

      } else {

        element.classList.add(
          "faded"
        );
      }


      el.appendChild(
        element
      );
    }
  );
}


function renderControls() {

  const el =
    document.getElementById(
      "controls"
    );

  el.innerHTML = "";


  if (state.phase === "lobby") {

    const button =
      document.createElement(
        "button"
      );

    button.textContent =
      "Spiel starten";

    button.onclick =
      startGame;

    el.appendChild(button);

    return;
  }


  if (
    state.phase === "choose_trump"
    &&
    state.current_player === myId
  ) {

    const title =
      document.createElement(
        "strong"
      );

    title.textContent =
      "Trumpf wählen:";

    el.appendChild(title);


    const grid =
      document.createElement(
        "div"
      );

    grid.className =
      "trump-grid";


    [
      "green",
      "blue",
      "yellow",
      "red"
    ].forEach(
      color => {

        const button =
          document.createElement(
            "button"
          );

        button.textContent =
          symbol(color);

        button.onclick =
          () => chooseTrump(
            color
          );

        grid.appendChild(
          button
        );
      }
    );


    el.appendChild(grid);

    return;
  }


  if (
    state.phase === "bidding"
    &&
    state.current_player === myId
  ) {

    const title =
      document.createElement(
        "strong"
      );

    title.textContent =
      "Deine Ansage:";

    el.appendChild(title);


    const grid =
      document.createElement(
        "div"
      );

    grid.className =
      "bid-grid";


    for (
      let i = 0;
      i <= state.cards_per_player;
      i++
    ) {

      const button =
        document.createElement(
          "button"
        );

      button.textContent =
        i;

      button.onclick =
        () => bid(i);

      grid.appendChild(
        button
      );
    }


    el.appendChild(grid);

    return;
  }


  if (
    state.phase === "round_result"
  ) {

    const button =
      document.createElement(
        "button"
      );

    button.textContent =
      "Nächste Runde";

    button.onclick =
      nextRound;

    el.appendChild(button);

    showRoundResult(el);

    return;
  }


  if (
    state.phase === "game_over"
  ) {

    showFinalScores(el);
  }
}


function showRoundResult(el) {

  const title =
    document.createElement(
      "h3"
    );

  title.textContent =
    "Rundenergebnis";

  el.appendChild(title);


  const table =
    document.createElement(
      "table"
    );

  table.className =
    "score-table";


  table.innerHTML = `
    <tr>
      <th>Spieler</th>
      <th>Ansage</th>
      <th>Stiche</th>
      <th>Runde</th>
      <th>Gesamt</th>
    </tr>
  `;


  state.players.forEach(
    player => {

      const row =
        table.insertRow();

      const roundPoints =
        player.round_scores[
          player.round_scores.length - 1
        ] ?? 0;


      row.innerHTML = `
        <td>
          ${escapeHtml(player.name)}
        </td>
        <td>
          ${player.bid}
        </td>
        <td>
          ${player.tricks_won}
        </td>
        <td>
          ${roundPoints >= 0 ? "+" : ""}
          ${roundPoints}
        </td>
        <td>
          ${player.score}
        </td>
      `;
    }
  );


  el.appendChild(table);
}


function showFinalScores(el) {

  const title =
    document.createElement(
      "h2"
    );

  title.textContent =
    "🏆 Endstand";

  el.appendChild(title);


  const sorted =
    [...state.players].sort(
      (a,b) =>
        b.score - a.score
    );


  const table =
    document.createElement(
      "table"
    );

  table.className =
    "score-table";


  table.innerHTML = `
    <tr>
      <th>Spieler</th>
      <th>Punkte</th>
    </tr>
  `;


  sorted.forEach(
    player => {

      const row =
        table.insertRow();

      row.innerHTML = `
        <td>
          ${escapeHtml(player.name)}
        </td>
        <td>
          ${player.score}
        </td>
      `;
    }
  );


  el.appendChild(table);
}


function makeCard(card, small) {

  const div =
    document.createElement(
      "div"
    );


  div.className =
    `card ${card.ui_color || ""}`
    + (
      small
        ? " small"
        : ""
    );


  if (
    card.type === "wizard"
  ) {

    div.innerHTML =
      "<div>🧙</div><small>Z</small>";

  } else if (
    card.type === "fool"
  ) {

    div.innerHTML =
      "<div>🤡</div><small>N</small>";

  } else {

    div.innerHTML =
      `<div>${card.value}</div>`
      + `<small>${symbol(card.color)}</small>`;
  }


  return div;
}


function symbol(color) {

  return {
    green:"🟢",
    blue:"🔵",
    yellow:"🟡",
    red:"🔴"
  }[color] || "";
}


function escapeHtml(value) {

  return String(value).replace(
    /[&<>"']/g,
    character => ({
      "&":"&amp;",
      "<":"&lt;",
      ">":"&gt;",
      '"':"&quot;",
      "'":"&#039;"
    }[character])
  );
}


connect();

</script>

</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTML


# ============================================================
# WEBSOCKET
# ============================================================

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):

    await websocket.accept()

    room = None
    player_id = None

    try:

        while True:

            message = await websocket.receive_json()

            action = message.get("action")


            # ------------------------------------------------
            # CREATE ROOM
            # ------------------------------------------------

            if action == "create_room":

                name = (
                    str(
                        message.get(
                            "name",
                            "Spieler"
                        )
                    ).strip()[:24]
                    or "Spieler"
                )

                bot_count = max(
                    1,
                    min(
                        5,
                        int(
                            message.get(
                                "bot_count",
                                1
                            )
                        )
                    )
                )

                room, player_id = (
                    rooms.create_room(
                        name,
                        bot_count
                    )
                )

                room.connections[
                    player_id
                ] = websocket

                await websocket.send_json({
                    "type": "identity",
                    "player_id": player_id,
                    "room": room.code
                })

                await room.send_state()


            # ------------------------------------------------
            # JOIN ROOM
            # ------------------------------------------------

            elif action == "join_room":

                code = (
                    str(
                        message.get(
                            "room",
                            ""
                        )
                    ).strip().upper()
                )

                name = (
                    str(
                        message.get(
                            "name",
                            "Spieler"
                        )
                    ).strip()[:24]
                    or "Spieler"
                )

                room = rooms.get(code)

                if room is None:

                    await websocket.send_json({
                        "type": "error",
                        "message":
                            "Raum nicht gefunden."
                    })

                    continue


                player_id = room.add_human(name)


                if player_id is None:

                    await websocket.send_json({
                        "type": "error",
                        "message":
                            "Raum ist voll "
                            "oder das Spiel läuft bereits."
                    })

                    continue


                room.connections[
                    player_id
                ] = websocket


                await websocket.send_json({
                    "type": "identity",
                    "player_id": player_id,
                    "room": room.code
                })


                await room.send_state()


            # ------------------------------------------------
            # START GAME
            # ------------------------------------------------

            elif action == "start_game":

                if not room or not player_id:
                    continue

                # In round_result this means "continue".
                if room.game.phase == "round_result":

                    if player_id == room.game.players[0].id:
                        room.game.continue_after_round()
                        await room.run_bots()

                else:

                    room.game.start(
                        player_id
                    )

                    await room.run_bots()


                await room.send_state()


            # ------------------------------------------------
            # NEXT ROUND
            # ------------------------------------------------

            elif action == "next_round":

                if (
                    room
                    and player_id
                    and room.game.phase == "round_result"
                    and player_id ==
                        room.game.players[0].id
                ):

                    room.game.continue_after_round()

                    await room.run_bots()

                    await room.send_state()


            # ------------------------------------------------
            # CHOOSE TRUMP
            # ------------------------------------------------

            elif action == "choose_trump":

                if room and player_id:

                    result = room.game.choose_trump(
                            player_id,
                            message.get("color")
                        )

                    if not result["ok"]:

                        await websocket.send_json({
                            "type": "error",
                            "message":
                                result["message"]
                        })

                    else:

                        await room.run_bots()

                    await room.send_state()


            # ------------------------------------------------
            # BID
            # ------------------------------------------------

            elif action == "bid":

                if room and player_id:

                    try:
                        bid_value = int(
                            message.get(
                                "bid",
                                -1
                            )
                        )
                    except (ValueError, TypeError):
                        bid_value = -1

                    result = room.game.submit_bid(
                            player_id,
                            bid_value
                        )

                    if not result["ok"]:

                        await websocket.send_json({
                            "type": "error",
                            "message":
                                result["message"]
                        })

                    else:

                        await room.run_bots()

                    await room.send_state()


            # ------------------------------------------------
            # PLAY CARD
            # ------------------------------------------------

            elif action == "play_card":

                if room and player_id:

                    result = room.game.play_card(
                            player_id,
                            message.get(
                                "card_id"
                            )
                        )

                    if not result["ok"]:

                        await websocket.send_json({
                            "type": "error",
                            "message":
                                result["message"]
                        })

                    else:

                        await room.run_bots()

                    await room.send_state()


    except WebSocketDisconnect:

        if room and player_id:
            room.connections.pop(
                player_id,
                None
            )
