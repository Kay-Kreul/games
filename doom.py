"""A tiny, self-contained Doom-style raycaster using only the Python standard library.
Run with: python doom.py
"""
import math
import io
import random
import socket
import struct
import sys
import threading
import time
import tkinter as tk
import wave

ctypes = None
winsound = None
if sys.platform == "win32":
  import ctypes
  import winsound

WIDTH, HEIGHT = 1280, 720

TILE = 64
player = [2.5 * TILE, 2.5 * TILE]
angle = 0.0
health = 100
score = 0
MAGAZINE_SIZE = 6
bullets = MAGAZINE_SIZE
shotgun_shells = 0
shotgun_unlocked = False
weapons = ("PISTOL", "SHOTGUN")
weapon_index = 0
ENEMY_STARTS = [(8.5, 3.5), (12.5, 7.5), (5.5, 8.5)]
network = None
remote_players = {}
network_lock = threading.Lock()


def network_loop(sock):
  """Exchange small JSON-like text packets without affecting the render loop."""
  sock.settimeout(0.15)
  while True:
    try:
      packet = sock.recv(4096)
      if not packet:
        break
      for line in packet.decode(errors="ignore").splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[0] == "P":
          with network_lock:
            remote_players[parts[1]] = (float(parts[2]), float(parts[3]),
                                        float(parts[4]) if len(parts) > 4 else 0)
    except socket.timeout:
      continue
    except (OSError, ValueError):
      break


def start_network():
  """Start LAN mode: `--host [port]` or `--join host [port]`."""
  global network
  try:
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    port = int(sys.argv[3] if mode == "--join" and len(sys.argv) > 3 else
               sys.argv[2] if mode == "--host" and len(sys.argv) > 2 else 4711)
    if mode == "--host":
      print(f"Hosting multiplayer on port {port}. Waiting for a player to join...")
      listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
      listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
      listener.bind(("0.0.0.0", port))
      listener.listen(1)
      listener.settimeout(10)
      network = listener.accept()[0]
      listener.close()
      print("Player connected. Starting game.")
    elif mode == "--join" and len(sys.argv) > 2:
      print(f"Joining multiplayer host {sys.argv[2]} on port {port}...")
      network = socket.create_connection((sys.argv[2], port), timeout=5)
      print("Connected to host. Starting game.")
    if network:
      threading.Thread(target=network_loop, args=(network,), daemon=True).start()
  except (OSError, ValueError, IndexError):
    print("Could not start multiplayer; continuing in singleplayer mode.")
    network = None


def select_game_mode():
  """Choose a game mode before creating the game window."""
  print("DOOM GAME MODE")
  print("1. Singleplayer")
  print("2. Multiplayer")
  try:
    choice = input("Select mode [1]: ").strip()
  except (EOFError, KeyboardInterrupt):
    choice = "1"
  if choice != "2":
    return

  print("MULTIPLAYER SETUP")
  print("One player chooses host; the other chooses join and enters the host's IP address.")
  print("Host command: python doom.py --host 4711")
  print("Join command: python doom.py --join HOST_IP 4711")

  if len(sys.argv) < 2 or sys.argv[1] not in ("--host", "--join"):
    try:
      mode = input("Host or join [host]: ").strip().lower() or "host"
      if mode == "join":
        print("Join setup: enter the host computer's IP address, then the same port as the host.")
        host = input("Host address: ").strip()
        port = input("Port [4711]: ").strip() or "4711"
        sys.argv[1:1] = ["--join", host, port]
      else:
        print("Host setup: enter a port, then give the other player this computer's IP address and port.")
        port = input("Port [4711]: ").strip() or "4711"
        sys.argv[1:1] = ["--host", port]
    except (EOFError, KeyboardInterrupt):
      return
  start_network()


def send_network_state():
  if network:
    try:
      network.sendall(f"P local {player[0]:.1f} {player[1]:.1f} {angle:.3f}\n".encode())
    except OSError:
      pass


def make_map():
  """Create a new random map, keeping a clear starting area and enemies."""
  width, height = 16, 11
  protected = {(x, y) for x, y in [(2, 2), (3, 2), (2, 3)]}
  protected.update((int(x), int(y)) for x, y in ENEMY_STARTS)
  rows = []
  for y in range(height):
    row = []
    for x in range(width):
      wall = x in (0, width - 1) or y in (0, height - 1)
      if not wall and (x, y) not in protected:
        wall = random.random() < 0.18
      row.append("1" if wall else ".")
    rows.append("".join(row))
  # Keep the initial routes and enemy cells usable on every generated map.
  for x, y in protected:
    rows[y] = rows[y][:x] + "." + rows[y][x + 1:]
  return rows


MAP = make_map()
enemies = [[x * TILE, y * TILE, 2] for x, y in ENEMY_STARTS]
pickup_cells = [(x, y) for y, row in enumerate(MAP) for x, cell in enumerate(row)
                if cell == "." and (x, y) not in {(2, 2), (3, 2), (2, 3)}
                and (x, y) not in {(int(ex), int(ey)) for ex, ey in ENEMY_STARTS}]
pickup_x, pickup_y = random.choice(pickup_cells)
shotgun_pickup = [pickup_x * TILE + TILE / 2, pickup_y * TILE + TILE / 2]
keys = set()
last_mouse_x = None
mouse_locked = False
look_velocity = 0.0
mouse_turn = 0.0
muzzle_flash = 0.0
pickup_bob = 0.0
gunshot_audio = None
last_tick_time = time.perf_counter()
strafe_velocity = 0.0
select_game_mode()
root = tk.Tk()

root.title("DOOM: The Python Experiment")
fullscreen = True
root.attributes("-fullscreen", fullscreen)
root.update_idletasks()
WIDTH, HEIGHT = root.winfo_screenwidth(), root.winfo_screenheight()

root.resizable(True, True)
canvas = tk.Canvas(root, width=WIDTH, height=HEIGHT, highlightthickness=0)
canvas.pack(fill="both", expand=True)


def toggle_fullscreen():
  """Toggle actual borderless fullscreen mode."""
  global fullscreen
  fullscreen = not fullscreen
  root.attributes("-fullscreen", fullscreen)
  if fullscreen:
    root.focus_force()


def blocked(x, y):
    gx, gy = int(x // TILE), int(y // TILE)
    return gy < 0 or gy >= len(MAP) or gx < 0 or gx >= len(MAP[0]) or MAP[gy][gx] == "1"


def view_origin():
  """Return the camera position, shifted sideways while leaning."""
  lean = -5 if "1" in keys else 5 if "2" in keys else 0
  return (player[0] + math.cos(angle + math.pi / 2) * lean,
          player[1] + math.sin(angle + math.pi / 2) * lean)



def visible(x, y):
  """Return whether a straight line to a point is clear of walls."""
  origin_x, origin_y = view_origin()
  dx, dy = x - origin_x, y - origin_y
  distance = math.hypot(dx, dy)
  steps = max(1, int(distance / 6))
  for step in range(1, steps):
    fraction = step / steps
    if blocked(origin_x + dx * fraction, origin_y + dy * fraction):
      return False
  return True


def move(dx, dy):
    if not blocked(player[0] + dx, player[1]):
      player[0] += dx
    if not blocked(player[0], player[1] + dy):
      player[1] += dy


def shoot():
  global score, bullets, shotgun_shells
  if weapon_index == 0:
    if bullets <= 0:
      return
    bullets -= 1
    damage = 2
    # Pistols only hit targets directly beneath the crosshair.
    hit_angle = 0.01
  else:
    if shotgun_shells <= 0:
      return
    shotgun_shells -= 1
    damage = 2
    hit_angle = 0.45
  # A hit is an enemy close to the crosshair and in front of the player.
  view_angle = angle - 0.08 if "1" in keys else angle + 0.08 if "2" in keys else angle
  origin_x, origin_y = view_origin()
  for enemy in enemies[:]:
    dx, dy = enemy[0] - origin_x, enemy[1] - origin_y
    distance = math.hypot(dx, dy)
    difference = abs((math.atan2(dy, dx) - view_angle + math.pi) % (2 * math.pi) - math.pi)
    if distance < 500 and difference < hit_angle and visible(enemy[0], enemy[1]):
      enemy[2] -= damage
      if enemy[2] <= 0:
        enemies.remove(enemy)
        score += 100
      if weapon_index == 0:
        return


def mouse_look(event):
  """Turn using horizontal mouse movement while the pointer is over the game."""
  global last_mouse_x, angle
  if not mouse_locked:
    return
  center_x = canvas.winfo_width() // 2
  if last_mouse_x is not None:
    delta = event.x - center_x
    # Ignore the tiny synthetic motion generated while recentering. Apply
    # real input immediately; queuing it until tick() made turns accumulate
    # behind the raycast render, especially while moving.
    if abs(delta) > 1:
      angle += delta * 0.00045
  last_mouse_x = center_x
  recenter_mouse()


def reset_mouse_tracking(event):
  global last_mouse_x, mouse_locked
  mouse_locked = True
  canvas.configure(cursor="none")
  event.widget.focus_set()
  last_mouse_x = canvas.winfo_width() // 2
  recenter_mouse()


def recenter_mouse():
  """Keep the pointer centered so horizontal looking has unlimited range."""
  if sys.platform == "win32" and mouse_locked and ctypes is not None:
    root.update_idletasks()
    x = root.winfo_rootx() + canvas.winfo_width() // 2
    y = root.winfo_rooty() + canvas.winfo_height() // 2
    ctypes.windll.user32.SetCursorPos(x, y)


def reload_weapon():
  """Refill the weapon's magazine."""
  global bullets
  bullets = MAGAZINE_SIZE


def switch_weapon(direction):
  global weapon_index
  if not shotgun_unlocked:
    weapon_index = 0
    return
  weapon_index = (weapon_index + direction) % len(weapons)


def fire(event=None):
  """Fire the weapon and provide a short synthesized gunshot sound."""
  global muzzle_flash, gunshot_audio
  if event is not None:
    event.widget.focus_set()
  if (weapon_index == 0 and bullets <= 0) or (weapon_index == 1 and shotgun_shells <= 0):
    return
  shoot()
  muzzle_flash = 0.10
  if sys.platform == "win32" and winsound is not None:
    # Cache the generated sound so shooting never stalls the render loop.
    if gunshot_audio is None:
      sample_rate, duration = 44100, 0.24
      samples = []
      for index in range(int(sample_rate * duration)):
        elapsed = index / sample_rate
        # A sharp transient, noisy blast, and decaying low-end body sound
        # more like a gunshot than a single constant-frequency beep.
        crack_envelope = max(0.0, 1.0 - elapsed / 0.035) ** 3
        body_envelope = max(0.0, 1.0 - elapsed / duration) ** 2
        crack = math.sin(2 * math.pi * (2400 - 900 * elapsed) * elapsed) * 0.35
        boom = math.sin(2 * math.pi * 72 * elapsed) * 0.95
        sub_boom = math.sin(2 * math.pi * 39 * elapsed) * 0.38
        noise = random.uniform(-1.0, 1.0) * (0.9 * crack_envelope + 0.18 * body_envelope)
        sample = crack_envelope * (noise + crack) + body_envelope * (boom + sub_boom)
        sample = max(-1.0, min(1.0, sample / 1.65))
        samples.append(struct.pack("<h", int(32767 * sample)))
      audio = io.BytesIO()
      with wave.open(audio, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(b"".join(samples))
      gunshot_audio = audio.getvalue()
    try:
      winsound.PlaySound(gunshot_audio, winsound.SND_MEMORY | winsound.SND_ASYNC)
    except (RuntimeError, OSError):
      # Some Windows audio devices reject memory playback; keep firing usable.
      root.bell()
  else:
    root.bell()


def handle_key_press(event):
  key = event.keysym.lower()
  keys.add(key)
  if key == "r":
    reload_weapon()
  elif key == "q":
    switch_weapon(-1)
  elif key == "e":
    switch_weapon(1)


def close_game(event):
  event.widget.winfo_toplevel().destroy()


def draw_world():
  canvas.delete("all")
  # Number keys roll the camera left or right.
  roll = 0.06 if "1" in keys else -0.06 if "2" in keys else 0
  view_angle = angle - 0.04 if "1" in keys else angle + 0.04 if "2" in keys else angle
  horizon = HEIGHT // 2
  canvas.create_rectangle(0, 0, WIDTH, horizon, fill="#101525", outline="")
  canvas.create_rectangle(0, horizon, WIDTH, HEIGHT, fill="#30252b", outline="")
  # Layered sky and floor bands add depth without external assets.
  for y in range(0, horizon, 18):
    shade = min(46, 18 + y // 14)
    canvas.create_rectangle(0, y, WIDTH, y + 18,
                            fill="#%02x%02x%02x" % (shade // 2, shade // 2, shade), outline="")
  for y in range(horizon, HEIGHT, 24):
    shade = min(64, 38 + (y - horizon) // 12)
    canvas.create_rectangle(0, y, WIDTH, y + 24,
                            fill="#%02x%02x%02x" % (shade, shade // 2, shade // 2), outline="")
  fov = math.pi / 3
  origin_x, origin_y = view_origin()
  for column in range(0, WIDTH, 2):
    column_horizon = horizon + int(roll * (column - WIDTH / 2))
    ray_angle = view_angle - fov / 2 + fov * column / WIDTH
    distance = 1
    while distance < 900:
      rx = origin_x + math.cos(ray_angle) * distance
      ry = origin_y + math.sin(ray_angle) * distance
      if blocked(rx, ry):
        break
      distance += 2
    distance *= math.cos(ray_angle - view_angle)
    wall_height = min(HEIGHT, int(42000 / max(distance, 1)))
    shade = max(25, min(210, int(22000 / max(distance, 1))))
    color = "#%02x%02x%02x" % (shade, shade // 2, shade // 3)
    canvas.create_rectangle(column, column_horizon - wall_height // 2, column + 2,
          column_horizon + wall_height // 2, fill=color, outline="")

  for ex, ey, _ in sorted(enemies, key=lambda e: -math.hypot(e[0] - player[0], e[1] - player[1])):
    dx, dy = ex - origin_x, ey - origin_y
    distance = math.hypot(dx, dy)
    relative = (math.atan2(dy, dx) - view_angle + math.pi) % (2 * math.pi) - math.pi
    if abs(relative) < fov / 2 and distance > 20 and visible(ex, ey):
      x = int(WIDTH / 2 + math.tan(relative) * WIDTH / (2 * math.tan(fov / 2)))
      size = max(12, int(24000 / distance))
      y = horizon - size // 2
      canvas.create_rectangle(x - size // 3, y, x + size // 3, y + size, fill="#8f1824", outline="#ff5360")
      # A more readable monster silhouette: horns, shoulders, arms, and boots.
      canvas.create_polygon(x - size // 3, y + size // 4,
             x - size // 2, y + size // 2, x - size // 3, y + 3 * size // 5,
             fill="#6e101b", outline="#ff5360")
      canvas.create_polygon(x + size // 3, y + size // 4,
             x + size // 2, y + size // 2, x + size // 3, y + 3 * size // 5,
             fill="#6e101b", outline="#ff5360")
      canvas.create_polygon(x - size // 4, y + size, x - size // 12, y + size,
             x - size // 12, y + 6 * size // 5, x - size // 3, y + 6 * size // 5,
             fill="#32141b", outline="#8f1824")
      canvas.create_polygon(x + size // 12, y + size, x + size // 4, y + size,
             x + size // 3, y + 6 * size // 5, x + size // 12, y + 6 * size // 5,
             fill="#32141b", outline="#8f1824")
      canvas.create_polygon(x - size // 4, y + size // 20, x - size // 7, y - size // 5,
             x - size // 12, y + size // 10, fill="#c4373d", outline="#ff5360")
      canvas.create_polygon(x + size // 4, y + size // 20, x + size // 7, y - size // 5,
             x + size // 12, y + size // 10, fill="#c4373d", outline="#ff5360")
      canvas.create_oval(x - size // 5, y + size // 20, x + size // 5,
                 y + 2 * size // 5, fill="#e6be78", outline="")
      eye_y = y + size // 5
      canvas.create_oval(x - size // 9, eye_y, x - size // 18, eye_y + size // 12,
             fill="#fff06a", outline="")
      canvas.create_oval(x + size // 18, eye_y, x + size // 9, eye_y + size // 12,
             fill="#fff06a", outline="")

  # Draw connected players as simple colored marine models.
  with network_lock:
    other_players = list(remote_players.values())
  for px, py, remote_angle in other_players:
    dx, dy = px - origin_x, py - origin_y
    distance = math.hypot(dx, dy)
    relative = (math.atan2(dy, dx) - view_angle + math.pi) % (2 * math.pi) - math.pi
    if abs(relative) < fov / 2 and distance > 20 and visible(px, py):
      x = int(WIDTH / 2 + math.tan(relative) * WIDTH / (2 * math.tan(fov / 2)))
      size = max(10, int(16000 / distance))
      y = horizon - size // 2
      canvas.create_oval(x - size // 4, y, x + size // 4, y + size // 2,
                         fill="#4fa3d1", outline="#bcecff", width=2)
      canvas.create_rectangle(x - size // 3, y + size // 2, x + size // 3,
                              y + size, fill="#28709b", outline="#bcecff")
      canvas.create_line(x, y + size // 2, x + int(math.cos(remote_angle) * size),
                         y + size // 2 + int(math.sin(remote_angle) * size),
                         fill="#ffe08a", width=max(2, size // 10))

  if shotgun_pickup is not None and visible(*shotgun_pickup):
    px, py = shotgun_pickup
    dx, dy = px - origin_x, py - origin_y
    distance = math.hypot(dx, dy)
    relative = (math.atan2(dy, dx) - view_angle + math.pi) % (2 * math.pi) - math.pi
    if abs(relative) < fov / 2 and distance > 20:
      x = int(WIDTH / 2 + math.tan(relative) * WIDTH / (2 * math.tan(fov / 2)))
      size = max(8, int(9000 / distance))
      y = horizon - size // 2 + int(math.sin(pickup_bob) * 8)
      # A hovering shotgun model, with a small shadow underneath.
      canvas.create_oval(x - size, y + size // 2, x + size, y + size * 2 // 3,
             fill="#16131a", outline="")
      canvas.create_rectangle(x - size, y, x + size // 3, y + size // 4,
              fill="#7b4a27", outline="#ffe08a", width=2)
      canvas.create_rectangle(x + size // 3, y - size // 12, x + size,
              y + size // 8, fill="#b87935", outline="#ffe08a", width=2)
      canvas.create_rectangle(x - size // 3, y + size // 4, x - size // 8,
              y + size // 2, fill="#4b2a1b", outline="#ffe08a")
      canvas.create_text(x, y - 12, text="SHOTGUN", fill="#ffe08a",
             font=("Consolas", 10, "bold"))

  # Keep the weapon visible even when no enemies are on screen.
  canvas.create_polygon(WIDTH // 2 - 72, HEIGHT, WIDTH // 2 - 48, horizon + 105,
    WIDTH // 2 + 48, horizon + 105, WIDTH // 2 + 72, HEIGHT,
    fill="#17171c", outline="#555565")
  canvas.create_rectangle(WIDTH // 2 - (28 if weapon_index else 18), horizon + 86,
      WIDTH // 2 + (28 if weapon_index else 18), horizon + 150,
      fill="#7b4a27" if weapon_index else "#4b4b58", outline="#d5a05c" if weapon_index else "#8b8b9b")
  if muzzle_flash > 0:
    canvas.create_polygon(WIDTH // 2, horizon + 84, WIDTH // 2 - 54, horizon + 8,
      WIDTH // 2 - 16, horizon + 70, WIDTH // 2 + 16, horizon - 4,
      WIDTH // 2 + 54, horizon + 84, fill="#ffd447", outline="#fff4a3")

  canvas.create_line(WIDTH // 2 - 10, horizon, WIDTH // 2 + 10, horizon, fill="#f5f5f5", width=2)
  canvas.create_line(WIDTH // 2, horizon - 10, WIDTH // 2, horizon + 10, fill="#f5f5f5", width=2)
  ammo = bullets if not weapon_index else shotgun_shells
  canvas.create_text(14, 14, anchor="nw", fill="#f5f5f5", font=("Consolas", 15, "bold"),
             text=f"SCORE {score:04d}   {weapons[weapon_index]}")
  # Health is shown as a readable bar in the lower-left HUD.
  health_value = max(0, min(100, health))
  health_left, health_top = 14, HEIGHT - 58
  health_width, health_height = 230, 18
  canvas.create_rectangle(health_left, health_top,
             health_left + health_width, health_top + health_height,
             fill="#252530", outline="#aaaabb", width=2)
  canvas.create_rectangle(health_left + 2, health_top + 2,
             health_left + 2 + (health_width - 4) * health_value / 100,
             health_top + health_height - 2,
             fill="#39b866" if health_value > 35 else "#d34b45", outline="")
  canvas.create_text(health_left + health_width // 2, health_top + health_height // 2,
             fill="#ffffff", font=("Consolas", 11, "bold"),
             text=f"HEALTH {int(health_value):03d}")
  canvas.create_text(WIDTH - 14, HEIGHT - 68, anchor="se", fill="#f5f5f5",
             font=("Consolas", 17, "bold"),
             text=f"AMMO {ammo:02d}")
  # Render individual rounds so the ammo count looks like actual bullets.
  bullet_count = MAGAZINE_SIZE if not weapon_index else 8
  bullet_y = HEIGHT - 30
  for index in range(bullet_count):
    x = WIDTH - 24 - index * 24
    loaded = index < ammo
    canvas.create_rectangle(x - 5, bullet_y - 15, x + 5, bullet_y + 7,
             fill="#d49a35" if loaded else "#3c3c46",
             outline="#ffe08a" if loaded else "#777783")
    canvas.create_oval(x - 5, bullet_y - 21, x + 5, bullet_y - 11,
             fill="#ffe08a" if loaded else "#555560", outline="")
  canvas.create_text(14, HEIGHT - 14, anchor="sw", fill="#d8d8e8",
             font=("Consolas", 11),
             text="Q/E SWITCH   W/S MOVE   A/D STRAFE   SPACE FIRE   R RELOAD")


def tick():
  global health, angle, mouse_turn, muzzle_flash, shotgun_pickup, shotgun_shells, shotgun_unlocked, pickup_bob, last_tick_time, strafe_velocity
  now = time.perf_counter()
  dt = min(0.05, max(0.001, now - last_tick_time))
  last_tick_time = now
  muzzle_flash = max(0.0, muzzle_flash - dt)
  pickup_bob += dt * 3
  forward = (("w" in keys) - ("s" in keys)) * 165 * dt
  # Smooth strafing so releasing or changing direction does not feel abrupt.
  target_strafe = (("d" in keys) - ("a" in keys)) * 110
  strafe_velocity += (target_strafe - strafe_velocity) * (1 - math.exp(-12 * dt))
  strafe = strafe_velocity * dt
  move(math.cos(angle) * forward + math.cos(angle + math.pi / 2) * strafe,
     math.sin(angle) * forward + math.sin(angle + math.pi / 2) * strafe)
  send_network_state()
  if shotgun_pickup is not None and math.hypot(shotgun_pickup[0] - player[0], shotgun_pickup[1] - player[1]) < 32:
    shotgun_pickup = None
    shotgun_unlocked = True
    shotgun_shells = 8
    switch_weapon(1)
  for enemy in enemies:
    if math.hypot(enemy[0] - player[0], enemy[1] - player[1]) < 120:
      health = max(0, health - 20 * dt)
  draw_world()
  if not enemies or health <= 0:
    canvas.create_text(WIDTH // 2, HEIGHT // 2, fill="#ffe45c", font=("Consolas", 42, "bold"),
               text=("YOU WIN!" if not enemies else "AHHHHHHHHHHHH") + "  (ESC to quit)")
  else:
    # Pace frames from the actual render time instead of accumulating timer drift.
    root.after(8, tick)


root.bind("<KeyPress>", handle_key_press)
root.bind("<KeyRelease>", lambda event: keys.discard(event.keysym.lower()))
canvas.bind("<Motion>", mouse_look)
canvas.bind("<Enter>", reset_mouse_tracking)
canvas.bind("<Button-1>", fire)
root.bind("<space>", fire)
root.bind("<F11>", toggle_fullscreen)
root.bind("<Escape>", close_game)
canvas.focus_set()
tick()
root.mainloop()


