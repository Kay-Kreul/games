"""A tiny, self-contained Hope-style raycaster using only the Python standard library.
Run with: python doom.py
"""
import math
import random
import socket
import sys
import threading
import time
import ctypes
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor

WIDTH, HEIGHT = 1280, 720

TILE = 64
MAP_SEED = random.SystemRandom().randint(0, 2**31 - 1)
PLAYER_RADIUS = 18
ENEMY_RADIUS = 20
player = [2.5 * TILE, 2.5 * TILE]
angle = 0.0
health = 100
score = 0
MAGAZINE_SIZE = 8
SHOTGUN_MAGAZINE_SIZE = 4
bullets = MAGAZINE_SIZE
shotgun_shells = 0
shotgun_unlocked = False
last_shotgun_shot = -0.5
weapons = ("PISTOL", "SHOTGUN")
weapon_index = 0
ENEMY_STARTS = [(8.5, 3.5), (12.5, 7.5), (5.5, 8.5)]
network = None
network_peers = []
network_peers_lock = threading.Lock()
remote_players = {}
network_lock = threading.Lock()
network_status = "SINGLEPLAYER"
remote_health = {}
remote_kills = {}
remote_names = {}
respawn_at = None
PLAYER_ID = f"p{random.SystemRandom().randint(0, 2**31 - 1):08x}"
network_peer_ids = {}
username = ""
multiplayer_spawned = False
DISCOVERY_PORT = 4712
discovered_hosts = {}
discovery_started = False
app_running = True


def scan_open_sockets():
  """Find reachable Hope hosts when UDP discovery is unavailable."""
  try:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.connect(("8.8.8.8", 80))
    local_ip = probe.getsockname()[0]
    probe.close()
    prefix = ".".join(local_ip.split(".")[:3])
  except OSError:
    prefix = "127.0.0"
  addresses = {"127.0.0.1"}
  addresses.update(f"{prefix}.{number}" for number in range(1, 255))

  def check(target):
    address, port = target
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.2)
    try:
      sock.connect((address, port))
      return address, port
    except OSError:
      return None
    finally:
      sock.close()

  targets = ((address, port) for address in addresses for port in range(4711, 4721))
  with ThreadPoolExecutor(max_workers=64) as pool:
    for result in pool.map(check, targets):
      if result:
        address, port = result
        discovered_hosts[f"{address}:{port}"] = (address, str(port))


def discover_hosts():
  """Listen for nearby Hope host announcements."""
  sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
  try:
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("", DISCOVERY_PORT))
    while True:
      data, address = sock.recvfrom(256)
      parts = data.decode(errors="ignore").split()
      if len(parts) == 3 and parts[0] == "HOPE":
        discovered_hosts[parts[1][:16]] = (address[0], parts[2])
  except OSError:
    pass
  finally:
    sock.close()


def announce_host(port):
  """Broadcast the host name and port for Quick Join."""
  sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
  try:
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    while network:
      sock.sendto(f"HOPE {username or 'Host'} {port}".encode(),
                  ("<broadcast>", DISCOVERY_PORT))
      time.sleep(2)
  except OSError:
    pass
  finally:
    sock.close()


def start_host_discovery():
  global discovery_started
  if not discovery_started:
    discovery_started = True
    threading.Thread(target=discover_hosts, daemon=True).start()
    threading.Thread(target=scan_open_sockets, daemon=True).start()


def network_loop(sock):
  """Exchange small JSON-like text packets without affecting the render loop."""
  global network_status, health, score, MAP_SEED, MAP, enemies, pickup_cells, shotgun_pickup
  sock.settimeout(0.15)
  buffer = ""
  network_status = "MULTIPLAYER"
  while True:
    try:
      packet = sock.recv(4096)
      if not packet:
        break
      buffer += packet.decode(errors="ignore")
      lines = buffer.split("\n")
      buffer = lines.pop()
      for line in lines:
        parts = line.split()
        if not parts or parts[0] not in ("P", "S", "M", "K"):
          continue
        # The host relays state and shots so clients that join after the game
        # has started can still see and damage the other players.
        if parts[0] in ("P", "S", "K"):
          with network_peers_lock:
            peers = list(network_peers)
          for peer in peers:
            if peer is not sock:
              try:
                peer.sendall((line + "\n").encode())
              except OSError:
                pass
        if parts[0] == "M":
          # The host is authoritative for the level seed.  Rebuild locally
          # when it arrives so both players see the same random map and item.
          if len(parts) < 2:
            continue
          try:
            MAP_SEED = int(parts[1])
          except ValueError:
            continue
          MAP = make_map()
          enemies = []
          pickup_cells = [(x, y) for y, row in enumerate(MAP) for x, cell in enumerate(row)
                          if cell == "." and (x, y) not in {(2, 2), (3, 2), (2, 3)}
                          and (x, y) not in {(int(ex), int(ey)) for ex, ey in ENEMY_STARTS}]
          pickup_x, pickup_y = random.Random(MAP_SEED + 1).choice(pickup_cells)
          shotgun_pickup = [pickup_x * TILE + TILE / 2, pickup_y * TILE + TILE / 2]
          continue
        if parts[0] == "S":
          if len(parts) < 4:
            continue
          try:
            shooter_angle = float(parts[2])
            weapon = int(parts[3])
          except ValueError:
            continue
          # The sender's latest position is used to validate a shot locally.
          with network_lock:
            shooter = remote_players.get(parts[1])
          if shooter is not None:
            dx, dy = player[0] - shooter[0], player[1] - shooter[1]
            distance = math.hypot(dx, dy)
            difference = abs((math.atan2(dy, dx) - shooter_angle + math.pi) %
                             (2 * math.pi) - math.pi)
            if visible(player[0], player[1]) and difference < (0.45 if weapon else 0.08):
              damage = 12 if not weapon else max(5, int(50 * max(0.0, 1 - distance / 500)))
              was_alive = health > 0
              health = max(0, health - damage)
              if was_alive and health <= 0:
                send_network_kill(parts[1], PLAYER_ID)
          continue
        if parts[0] == "K":
          if len(parts) >= 3:
            killer_id = parts[1]
            if killer_id == PLAYER_ID:
              # The kill packet updates the killer immediately; the next
              # position packet will also advertise this updated total.
              score += 100
            else:
              # Update other players' leaderboards as soon as the death is
              # received, without waiting for another position update.
              with network_lock:
                remote_kills[killer_id] = remote_kills.get(killer_id, 0) + 1
          continue
        if len(parts) < 4:
          continue
        try:
          state = (float(parts[2]), float(parts[3]),
                   float(parts[4]) if len(parts) > 4 else 0.0)
          remote_player_health = float(parts[5]) if len(parts) > 5 else 100.0
          remote_player_kills = int(float(parts[6])) if len(parts) > 6 else 0
          remote_username = parts[7][:16] if len(parts) > 7 else parts[1]
        except ValueError:
          continue
        with network_lock:
          network_peer_ids[sock] = parts[1]
          remote_players[parts[1]] = state
          remote_health[parts[1]] = remote_player_health
          remote_kills[parts[1]] = remote_player_kills
          remote_names[parts[1]] = remote_username
    except socket.timeout:
      continue
    except OSError:
      break
  network_status = "DISCONNECTED"
  with network_peers_lock:
    if sock in network_peers:
      network_peers.remove(sock)
  # Remove disconnected players, allowing the same user to reconnect without
  # leaving a stale player visible in the current match.
  with network_lock:
    player_id = network_peer_ids.pop(sock, None)
    if player_id:
      remote_players.pop(player_id, None)
      remote_health.pop(player_id, None)
      remote_kills.pop(player_id, None)
      remote_names.pop(player_id, None)


def accept_late_players(listener):
  """Keep the host open so additional players can join during a game."""
  global network_status
  while True:
    try:
      sock, _ = listener.accept()
      sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
      sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
      with network_peers_lock:
        network_peers.append(sock)
      sock.sendall(f"M {MAP_SEED}\n".encode())
      threading.Thread(target=network_loop, args=(sock,), daemon=True).start()
    except OSError:
      break


def start_network():
  """Start LAN mode: `--host [port]` or `--join host [port]`."""
  global network, network_status, enemies, network_peers
  try:
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    port = int(sys.argv[3] if mode == "--join" and len(sys.argv) > 3 else
               sys.argv[2] if mode == "--host" and len(sys.argv) > 2 else 4711)
    if mode == "--host":
      print(f"Hosting multiplayer on port {port}. Waiting for a player to join...")
      listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
      listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
      listener.bind(("0.0.0.0", port))
      listener.listen(16)
      # Allow the joining player time to enter the host address and connect.
      listener.settimeout(60)
      network = listener.accept()[0]
      # The initial accept has a timeout only to avoid waiting forever for the
      # first player; late joins and reconnects should remain possible.
      listener.settimeout(None)
      with network_peers_lock:
        network_peers.append(network)
      threading.Thread(target=accept_late_players, args=(listener,), daemon=True).start()
      print("Player connected. Starting game.")
    elif mode == "--join" and len(sys.argv) > 2:
      host = sys.argv[2].strip()
      if not host:
        raise ValueError("missing host address")
      print(f"Joining multiplayer host {host} on port {port}...")
      network = socket.create_connection((host, port), timeout=10)
      print("Connected to host. Starting game.")
    if network:
      enemies = []
      # Position packets are small and should be sent immediately.
      network.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
      network.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
      network_status = "CONNECTING"
      with network_peers_lock:
        if network not in network_peers:
          network_peers.append(network)
      # The host owns the seed; the joining client replaces its local seed
      # when this packet is received by network_loop().
      if mode == "--host":
        network.sendall(f"M {MAP_SEED}\n".encode())
        threading.Thread(target=announce_host, args=(port,), daemon=True).start()
      threading.Thread(target=network_loop, args=(network,), daemon=True).start()
  except (OSError, ValueError, IndexError, socket.timeout):
    print("Failed to connect; continuing in singleplayer mode.")
    network = None
    network_status = "SINGLEPLAYER"


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
      packet = f"P {PLAYER_ID} {player[0]:.1f} {player[1]:.1f} {angle:.3f} {health:.1f} {score // 100} {username}\n".encode()
      with network_peers_lock:
        peers = list(network_peers)
      for peer in peers:
        peer.sendall(packet)
    except OSError:
      pass


def send_network_shot():
  if network:
    try:
      packet = f"S {PLAYER_ID} {angle:.3f} {weapon_index}\n".encode()
      with network_peers_lock:
        peers = list(network_peers)
      for peer in peers:
        peer.sendall(packet)
    except OSError:
      pass


def send_network_kill(killer_id, victim_id):
  if network:
    try:
      packet = f"K {killer_id} {victim_id}\n".encode()
      with network_peers_lock:
        peers = list(network_peers)
      for peer in peers:
        peer.sendall(packet)
    except OSError:
      pass


def spawn_player():
  """Choose a clear multiplayer spawn away from other players."""
  global player
  cells = [(x, y) for y, row in enumerate(MAP) for x, cell in enumerate(row)
           if cell == "." and (x, y) not in {(2, 2), (3, 2), (2, 3)}]
  randomizer = random.SystemRandom()
  randomizer.shuffle(cells)
  with network_lock:
    occupied = list(remote_players.values())
  for x, y in cells:
    px, py = x * TILE + TILE / 2, y * TILE + TILE / 2
    if all(math.hypot(px - other[0], py - other[1]) > 2 * PLAYER_RADIUS + 80
           for other in occupied):
      player = [px, py]
      return


def make_map():
  """Create a new random map, keeping a clear starting area and enemies."""
  # Every client must generate the exact same level layout.  Do not use the
  # process-global RNG here because its state can differ between players.
  map_random = random.Random(MAP_SEED)
  width, height = 16, 11
  protected = {(x, y) for x, y in [(2, 2), (3, 2), (2, 3)]}
  protected.update((int(x), int(y)) for x, y in ENEMY_STARTS)
  rows = []
  for y in range(height):
    row = []
    for x in range(width):
      wall = x in (0, width - 1) or y in (0, height - 1)
      if not wall and (x, y) not in protected:
        wall = map_random.random() < 0.18
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
# Use a separate deterministic choice so both clients place the item alike,
# regardless of any other random calls made during the game.
pickup_x, pickup_y = random.Random(MAP_SEED + 1).choice(pickup_cells)
shotgun_pickup = [pickup_x * TILE + TILE / 2, pickup_y * TILE + TILE / 2]
game_over = False
end_button = None
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
root = tk.Tk()

root.title("HOPE: The Python Experiment")
fullscreen = True
root.attributes("-fullscreen", fullscreen)
root.update_idletasks()
WIDTH, HEIGHT = root.winfo_screenwidth(), root.winfo_screenheight()

root.resizable(True, True)
canvas = tk.Canvas(root, width=WIDTH, height=HEIGHT, highlightthickness=0)
canvas.pack(fill="both", expand=True)


def show_start_menu():
  """Choose the game mode in the game window before entering the map."""
  start_host_discovery()
  menu = tk.Toplevel(root)
  menu.title("HOPE GAME MODE")
  menu.geometry("520x520")
  menu.update_idletasks()
  menu.geometry("+%d+%d" % ((menu.winfo_screenwidth() - 520) // 2,
                            (menu.winfo_screenheight() - 520) // 2))
  menu.resizable(False, False)
  menu.transient(root)
  menu.grab_set()
  menu.focus_force()
  tk.Label(menu, text="Choose a game mode", font=("Consolas", 20, "bold")).pack(pady=18)

  tk.Label(menu, text="Username").pack()
  username_entry = tk.Entry(menu, width=25)
  username_entry.pack(pady=2)
  username_error = tk.Label(menu, text="Username is required", fg="#b00020")

  def set_username():
    global username
    value = username_entry.get().strip()[:16]
    if not value:
      username_error.pack()
      username_entry.focus_set()
      return False
    username = value
    username_error.pack_forget()
    return True

  def singleplayer():
    if not set_username():
      return
    menu.destroy()

  def multiplayer():
    if not set_username():
      return
    port = port_entry.get().strip() or "4711"
    if mode.get() == "host":
      sys.argv[1:1] = ["--host", port]
    else:
      sys.argv[1:1] = ["--join", host_entry.get().strip() or "127.0.0.1", port]
    menu.destroy()
    start_network()

  tk.Button(menu, text="Singleplayer", width=30, height=2, command=singleplayer).pack(pady=5)
  mode = tk.StringVar(value="host")
  tk.Radiobutton(menu, text="Host", variable=mode, value="host").pack()
  tk.Radiobutton(menu, text="Join", variable=mode, value="join").pack()
  def update_multiplayer_button(*_):
    multiplayer_button.config(text=("Start Multiplayer" if mode.get() == "host"
                                    else "Join Multiplayer"))

  mode.trace_add("write", update_multiplayer_button)
  host_entry = tk.Entry(menu, width=32)
  host_entry.insert(0, "127.0.0.1")
  host_entry.pack(pady=2)
  port_entry = tk.Entry(menu, width=12)
  port_entry.insert(0, "4711")
  port_entry.pack(pady=2)
  multiplayer_button = tk.Button(menu, text="Start Multiplayer", width=30, height=2,
                                 command=multiplayer)
  multiplayer_button.pack(pady=6)

  quick_join_status = tk.Label(menu, text="Searching for nearby hosts...", fg="#555555")
  quick_join_status.pack(pady=(10, 3))

  def refresh_quick_join_status():
    """Keep the nearby-host list and Quick Join status current."""
    if not menu.winfo_exists():
      return
    if discovered_hosts:
      hosts = list(discovered_hosts.items())
      host_name, (host, port) = hosts[0]
      quick_join_status.config(
          text=f"{len(hosts)} host(s) found; ready to join {host_name} ({host}:{port})",
          fg="#176b2c")
      quick_join_button.config(state="normal")
    else:
      quick_join_status.config(text="Searching for nearby hosts...", fg="#555555")
      quick_join_button.config(state="disabled")
    menu.after(1000, refresh_quick_join_status)

  def quick_join():
    """Join the first host announced on the local network."""
    if not discovered_hosts:
      quick_join_status.config(text="No nearby hosts found; keep waiting or enter an address above.",
                               fg="#b00020")
      return
    _, (host, port) = next(iter(discovered_hosts.items()))
    if not set_username():
      return
    sys.argv[1:1] = ["--join", host, str(port)]
    menu.destroy()
    start_network()

  quick_join_button = tk.Button(menu, text="Quick Join", width=30, height=2,
                                command=quick_join, state="disabled")
  quick_join_button.pack(pady=4)
  refresh_quick_join_status()
  tk.Label(menu, text="Quick Join uses a host found on your local network.",
           font=("Consolas", 9), fg="#666666").pack(pady=2)
  menu.protocol("WM_DELETE_WINDOW", root.destroy)
  root.wait_window(menu)


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


def circle_hits_wall(x, y, radius):
  """Use a true circular hitbox against nearby wall rectangles."""
  left = max(0, int((x - radius) // TILE) - 1)
  right = min(len(MAP[0]) - 1, int((x + radius) // TILE) + 1)
  top = max(0, int((y - radius) // TILE) - 1)
  bottom = min(len(MAP) - 1, int((y + radius) // TILE) + 1)
  for gy in range(top, bottom + 1):
    for gx in range(left, right + 1):
      if MAP[gy][gx] != "1":
        continue
      nearest_x = max(gx * TILE, min(x, (gx + 1) * TILE))
      nearest_y = max(gy * TILE, min(y, (gy + 1) * TILE))
      if math.hypot(x - nearest_x, y - nearest_y) < radius:
        return True
  return False


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
  if not circle_hits_wall(player[0] + dx, player[1], PLAYER_RADIUS):
      player[0] += dx
  if not circle_hits_wall(player[0], player[1] + dy, PLAYER_RADIUS):
      player[1] += dy


def shoot():
  global score, bullets, shotgun_shells, last_shotgun_shot
  if weapon_index == 0:
    if bullets <= 0:
      return
    bullets -= 1
    damage = 12
    # Pistols only hit targets directly beneath the crosshair.
    hit_angle = 0.01
  else:
    if shotgun_shells <= 0:
      return
    now = time.perf_counter()
    if now - last_shotgun_shot < 0.5:
      return
    last_shotgun_shot = now
    shotgun_shells -= 1
    damage = 50
    hit_angle = 0.45
  # A hit is an enemy close to the crosshair and in front of the player.
  view_angle = angle - 0.08 if "1" in keys else angle + 0.08 if "2" in keys else angle
  origin_x, origin_y = view_origin()
  for enemy in enemies[:]:
    dx, dy = enemy[0] - origin_x, enemy[1] - origin_y
    distance = math.hypot(dx, dy)
    difference = abs((math.atan2(dy, dx) - view_angle + math.pi) % (2 * math.pi) - math.pi)
    if (PLAYER_RADIUS + ENEMY_RADIUS <= distance < 500 and
      difference < hit_angle and visible(enemy[0], enemy[1])):
      actual_damage = damage
      if weapon_index == 1:
        actual_damage = max(5, int(damage * max(0.0, 1 - distance / 500)))
      enemy[2] -= actual_damage
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
    # Use movement between consecutive events so reversing direction responds
    # immediately instead of waiting to cross the original center position.
    if abs(delta) > 1:
      angle += delta * 0.00045
      recenter_mouse()
  last_mouse_x = center_x


def reset_mouse_tracking(event):
  event.widget.focus_set()
  canvas.grab_set()
  lock_mouse()


def lock_mouse():
  global last_mouse_x, mouse_locked
  mouse_locked = True
  canvas.configure(cursor="none")
  canvas.focus_set()
  last_mouse_x = canvas.winfo_width() // 2
  recenter_mouse()


def unlock_mouse():
  """Release mouse capture so the end-of-level button can be selected."""
  global mouse_locked, last_mouse_x
  mouse_locked = False
  last_mouse_x = None
  canvas.configure(cursor="")
  canvas.grab_release()


def recenter_mouse():
  """Keep the pointer centered so horizontal looking has unlimited range."""
  global last_mouse_x
  center_x = canvas.winfo_width() // 2
  center_y = canvas.winfo_height() // 2
  screen_x = canvas.winfo_rootx() + center_x
  screen_y = canvas.winfo_rooty() + center_y
  # Reposition the Windows cursor after every movement so it cannot leave the
  # game window while looking around.
  try:
    ctypes.windll.user32.SetCursorPos(screen_x, screen_y)
  except AttributeError:
    pass
  last_mouse_x = center_x


def reload_weapon():
  """Refill the currently selected weapon."""
  global bullets, shotgun_shells
  if weapon_index == 0:
    bullets = MAGAZINE_SIZE
  elif shotgun_unlocked:
    shotgun_shells = SHOTGUN_MAGAZINE_SIZE


def switch_weapon(direction):
  """Cycle weapons, keeping the shotgun unavailable until collected."""
  global weapon_index
  if not shotgun_unlocked:
    weapon_index = 0
  else:
    weapon_index = (weapon_index + direction) % len(weapons)


def fire(*_):
  """Fire from mouse or keyboard input."""
  global muzzle_flash
  if game_over:
    return
  ammo_before = (bullets, shotgun_shells)
  shoot()
  if (bullets, shotgun_shells) != ammo_before:
    muzzle_flash = 0.12
    send_network_shot()


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
  global app_running
  app_running = False
  event.widget.winfo_toplevel().destroy()


def reset_level():
  global player, angle, health, score, bullets, shotgun_shells, respawn_at
  global MAP, MAP_SEED, pickup_cells
  global shotgun_unlocked, weapon_index, enemies, shotgun_pickup, game_over, last_shotgun_shot
  if not network:
    # Use a fresh seed for each single-player restart so the level changes.
    MAP_SEED = random.SystemRandom().randint(0, 2**31 - 1)
    MAP = make_map()
    pickup_cells = [(x, y) for y, row in enumerate(MAP) for x, cell in enumerate(row)
                    if cell == "." and (x, y) not in {(2, 2), (3, 2), (2, 3)}
                    and (x, y) not in {(int(ex), int(ey)) for ex, ey in ENEMY_STARTS}]
  player = [2.5 * TILE, 2.5 * TILE]
  angle, health, score = 0.0, 100, 0
  bullets, shotgun_shells = MAGAZINE_SIZE, 0
  last_shotgun_shot = -0.5
  shotgun_unlocked, weapon_index = False, 0
  enemies = [] if network else [[x * TILE, y * TILE, 2] for x, y in ENEMY_STARTS]
  pickup_x, pickup_y = random.Random(MAP_SEED + 1).choice(pickup_cells)
  shotgun_pickup = [pickup_x * TILE + TILE / 2, pickup_y * TILE + TILE / 2]
  game_over = False
  respawn_at = None
  lock_mouse()
  tick()


def respawn_player():
  """Place a dead multiplayer player at a random clear map cell."""
  global player, health, respawn_at
  cells = [(x, y) for y, row in enumerate(MAP) for x, cell in enumerate(row)
           if cell == "." and math.hypot(x * TILE + TILE / 2 - player[0],
                                         y * TILE + TILE / 2 - player[1]) > 160]
  if cells:
    x, y = random.choice(cells)
    player = [x * TILE + TILE / 2, y * TILE + TILE / 2]
  health = 100
  respawn_at = None


def retry_level():
  """Respawn the player at a random clear location after dying."""
  global game_over
  respawn_player()
  game_over = False
  lock_mouse()
  tick()


def end_button_click(event):
  if end_button is not None:
    x1, y1, x2, y2, command = end_button
    if x1 <= event.x <= x2 and y1 <= event.y <= y2:
      command()


def draw_graphics_backdrop(horizon):
  """Paint a richer atmospheric backdrop without external assets."""
  # Deep color gradients for a less flat, more dramatic scene.
  for y in range(horizon):
    t = y / max(1, horizon)
    r, g, b = int(7 + 18 * t), int(10 + 17 * t), int(25 + 35 * t)
    canvas.create_line(0, y, WIDTH, y, fill="#%02x%02x%02x" % (r, g, b))
  for y in range(horizon, HEIGHT):
    t = (y - horizon) / max(1, HEIGHT - horizon)
    r, g, b = int(25 + 38 * t), int(20 + 25 * t), int(28 + 22 * t)
    canvas.create_line(0, y, WIDTH, y, fill="#%02x%02x%02x" % (r, g, b))
  # Stars, distant haze, and overhead light shafts give the map depth.
  for index in range(34):
    x = (index * 347 + MAP_SEED % 271) % max(1, WIDTH)
    y = 25 + (index * 53 + MAP_SEED % 97) % max(30, horizon - 45)
    size = 1 + index % 2
    canvas.create_oval(x, y, x + size, y + size, fill="#7786ad", outline="")
  for x in range(-WIDTH, WIDTH * 2, 150):
    canvas.create_polygon(x, horizon - 35, x + 70, horizon - 35,
                          x + 260, HEIGHT, x + 80, HEIGHT,
                          fill="#3b2935", outline="")
  canvas.create_oval(WIDTH // 2 - 72, horizon - 170, WIDTH // 2 + 72,
                     horizon - 26, fill="#493447", outline="")


def draw_vignette():
  """Add subtle screen edges and a cinematic crosshair."""
  edge = 90
  for i in range(5):
    alpha = 5 + i * 4
    color = "#%02x%02x%02x" % (alpha, alpha, alpha + 5)
    canvas.create_rectangle(i * edge // 5, i * edge // 5,
                            WIDTH - i * edge // 5, HEIGHT - i * edge // 5,
                            outline=color, width=2)
  canvas.create_oval(WIDTH // 2 - 15, HEIGHT // 2 - 15,
                     WIDTH // 2 + 15, HEIGHT // 2 + 15,
                     outline="#a9d6d0", width=1)


def draw_world():
  global end_button
  end_button = None
  canvas.delete("all")
  # Number keys roll the camera left or right.
  roll = 0.06 if "1" in keys else -0.06 if "2" in keys else 0
  view_angle = angle - 0.04 if "1" in keys else angle + 0.04 if "2" in keys else angle
  horizon = HEIGHT // 2
  draw_graphics_backdrop(horizon)
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
    shade = max(22, min(220, int(24500 / max(distance, 1))))
    # Alternating bands suggest masonry panels and make walls read as 3-D.
    band = (int(distance // 18) + column // 28) % 3
    color = ("#%02x%02x%02x" % (shade, shade // 2, shade // 3) if band else
             "#%02x%02x%02x" % (min(255, shade + 18), shade // 2, shade // 4))
    canvas.create_rectangle(column, column_horizon - wall_height // 2, column + 2,
          column_horizon + wall_height // 2, fill=color, outline="")

  # Floor perspective grid: inexpensive geometry with a strong visual payoff.
  for depth in range(1, 14):
    y = horizon + int((HEIGHT - horizon) * (depth / 14) ** 1.7)
    canvas.create_line(0, y, WIDTH, y, fill="#49343c", width=1)
  for grid_x in range(-WIDTH, WIDTH * 2, 96):
    canvas.create_line(WIDTH // 2, horizon, grid_x, HEIGHT, fill="#3b2c35", width=1)

  for ex, ey, _ in sorted(enemies, key=lambda e: -math.hypot(e[0] - player[0], e[1] - player[1])):
    dx, dy = ex - origin_x, ey - origin_y
    distance = math.hypot(dx, dy)
    relative = (math.atan2(dy, dx) - view_angle + math.pi) % (2 * math.pi) - math.pi
    if abs(relative) < fov / 2 and distance > 20 and visible(ex, ey):
      x = int(WIDTH / 2 + math.tan(relative) * WIDTH / (2 * math.tan(fov / 2)))
      size = max(12, int(24000 / distance))
      y = horizon - size // 2
      # Layered demon model with a distinct body, armor, core, and face.
      canvas.create_oval(x - size // 3, y - size // 10, x + size // 3,
             y + size // 2, fill="#4b0e18", outline="#ff5360", width=2)
      canvas.create_rectangle(x - size // 3, y + size // 8, x + size // 3,
              y + size, fill="#8f1824", outline="#ff5360", width=2)
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
      canvas.create_polygon(x - size // 8, y + size // 3, x, y + size // 5,
                x + size // 8, y + size // 3, x, y + size // 2,
                fill="#ffb52e", outline="#fff06a")
      eye_y = y + size // 5
      canvas.create_oval(x - size // 9, eye_y, x - size // 18, eye_y + size // 12,
             fill="#fff06a", outline="")
      canvas.create_oval(x + size // 18, eye_y, x + size // 9, eye_y + size // 12,
             fill="#fff06a", outline="")
      canvas.create_line(x - size // 5, y + size // 2, x + size // 5,
             y + size // 2, fill="#ff5360", width=max(1, size // 18))

  # Draw connected players as simple colored marine models.
  with network_lock:
    other_players = [(player_id, state, remote_health.get(player_id, 100))
                     for player_id, state in remote_players.items()]
  for _, (px, py, remote_angle), player_health in other_players:
    # Dead network players remain hidden until their respawn packet arrives.
    if player_health <= 0:
      continue
    dx, dy = px - origin_x, py - origin_y
    distance = math.hypot(dx, dy)
    relative = (math.atan2(dy, dx) - view_angle + math.pi) % (2 * math.pi) - math.pi
    if abs(relative) < fov / 2 and distance > 20 and visible(px, py):
      x = int(WIDTH / 2 + math.tan(relative) * WIDTH / (2 * math.tan(fov / 2)))
      size = max(10, int(16000 / distance))
      y = horizon - size // 2
      canvas.create_oval(x - size // 4, y, x + size // 4, y + size // 2,
             fill="#b9c9d3", outline="#efffff", width=2)
      canvas.create_rectangle(x - size // 5, y + size // 8, x + size // 5,
             y + size // 4, fill="#142331", outline="#8de4ff", width=2)
      canvas.create_rectangle(x - size // 5, y + size // 8, x + size // 5,
              y + size // 3, fill="#263d50", outline="#8de4ff")
      canvas.create_rectangle(x - size // 3, y + size // 2, x + size // 3,
              y + size, fill="#28709b", outline="#bcecff", width=2)
      canvas.create_polygon(x - size // 5, y + size // 3, x - size // 2,
            y + size // 2, x - size // 3, y + size * 3 // 5,
              fill="#3d8fba", outline="#bcecff")
      canvas.create_polygon(x + size // 5, y + size // 3, x + size // 3,
              y + size // 2, x + size // 3, y + size * 3 // 5,
              fill="#3d8fba", outline="#bcecff")
      canvas.create_line(x - size // 6, y + size, x - size // 6, y + size * 6 // 5,
             fill="#182b3a", width=max(3, size // 8))
      canvas.create_line(x + size // 6, y + size, x + size // 6, y + size * 6 // 5,
             fill="#182b3a", width=max(3, size // 8))
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

  # Kills leaderboard in the upper-left corner.
  with network_lock:
    standings = [(username, score // 100)] + [
      (remote_names.get(player_id, player_id), kills)
      for player_id, kills in remote_kills.items()]
  standings.sort(key=lambda item: (-item[1], item[0]))
  board_x, board_y = 12, 44
  board_width = 190
  board_height = 30 + 19 * len(standings)
  canvas.create_rectangle(board_x, board_y, board_x + board_width,
             board_y + board_height, fill="#10131c", outline="#687080", width=2)
  canvas.create_text(board_x + 8, board_y + 5, anchor="nw", fill="#ffe45c",
             font=("Consolas", 11, "bold"), text="KILLS")
  for row, (name, kills) in enumerate(standings):
    y = board_y + 25 + row * 19
    canvas.create_text(board_x + 8, y, anchor="nw", fill="#ffffff",
             font=("Consolas", 10), text=f"{row + 1}. {name[:12]}")
    canvas.create_text(board_x + board_width - 8, y, anchor="ne", fill="#ffffff",
             font=("Consolas", 10, "bold"), text=str(kills))

  # Small top-right map showing walls, players, and enemies.
  map_scale = 10
  map_width = len(MAP[0]) * map_scale
  map_height = len(MAP) * map_scale
  map_x = WIDTH - map_width - 18
  map_y = 44
  canvas.create_rectangle(map_x - 5, map_y - 5, map_x + map_width + 5,
             map_y + map_height + 5, fill="#10131c", outline="#687080", width=2)
  for map_row, map_data in enumerate(MAP):
    for map_col, cell in enumerate(map_data):
      if cell == "1":
        canvas.create_rectangle(map_x + map_col * map_scale, map_y + map_row * map_scale,
             map_x + (map_col + 1) * map_scale, map_y + (map_row + 1) * map_scale,
             fill="#586171", outline="")

  def minimap_position(world_x, world_y):
    return (map_x + world_x / TILE * map_scale,
            map_y + world_y / TILE * map_scale)

  local_x, local_y = minimap_position(*player)
  canvas.create_oval(local_x - 3, local_y - 3, local_x + 3, local_y + 3,
             fill="#8ff0a4", outline="")
  canvas.create_line(local_x, local_y,
             local_x + math.cos(angle) * 8, local_y + math.sin(angle) * 8,
             fill="#8ff0a4", width=2)
  for enemy_x, enemy_y, _ in enemies:
    marker_x, marker_y = minimap_position(enemy_x, enemy_y)
    canvas.create_oval(marker_x - 2, marker_y - 2, marker_x + 2, marker_y + 2,
             fill="#ff5360", outline="")
  with network_lock:
    connected_players = list(remote_players.values())
  for remote_x, remote_y, _ in connected_players:
    marker_x, marker_y = minimap_position(remote_x, remote_y)
    canvas.create_oval(marker_x - 2, marker_y - 2, marker_x + 2, marker_y + 2,
             fill="#8de4ff", outline="")

  canvas.create_text(14, 14, anchor="nw", fill="#f5f5f5", font=("Consolas", 15, "bold"),
             text=f"SCORE {score:04d}   {weapons[weapon_index]}")
  canvas.create_text(WIDTH - 14, 14, anchor="ne",
             fill="#8ff0a4" if network_status == "MULTIPLAYER" else "#bbbbc5",
             font=("Consolas", 12, "bold"), text=network_status)
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
  bullet_count = MAGAZINE_SIZE if not weapon_index else SHOTGUN_MAGAZINE_SIZE
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
  draw_vignette()


def tick():
  global health, angle, mouse_turn, muzzle_flash, shotgun_pickup, shotgun_shells, shotgun_unlocked, pickup_bob, last_tick_time, strafe_velocity, game_over, end_button, respawn_at, multiplayer_spawned
  if not app_running:
    return
  try:
    if not root.winfo_exists():
      return
  except tk.TclError:
    return
  now = time.perf_counter()
  dt = min(0.05, max(0.001, now - last_tick_time))
  last_tick_time = now
  muzzle_flash = max(0.0, muzzle_flash - dt)
  pickup_bob += dt * 3
  if network and health <= 0:
    if respawn_at is None:
      respawn_at = now + 2.0
    if now >= respawn_at:
      respawn_player()
  if network and health > 0 and not multiplayer_spawned:
    spawn_player()
    multiplayer_spawned = True
  forward = (("w" in keys) - ("s" in keys)) * 165 * dt if health > 0 else 0
  # Smooth strafing so releasing or changing direction does not feel abrupt.
  target_strafe = (("d" in keys) - ("a" in keys)) * 110
  strafe_velocity += (target_strafe - strafe_velocity) * (1 - math.exp(-12 * dt))
  strafe = strafe_velocity * dt
  move(math.cos(angle) * forward + math.cos(angle + math.pi / 2) * strafe,
     math.sin(angle) * forward + math.sin(angle + math.pi / 2) * strafe)
  send_network_state()
  if health > 0 and shotgun_pickup is not None and math.hypot(shotgun_pickup[0] - player[0], shotgun_pickup[1] - player[1]) < 32:
    shotgun_pickup = None
    shotgun_unlocked = True
    shotgun_shells = SHOTGUN_MAGAZINE_SIZE
    switch_weapon(1)
  for enemy in enemies:
    if math.hypot(enemy[0] - player[0], enemy[1] - player[1]) < PLAYER_RADIUS + ENEMY_RADIUS + 18:
      if health > 0:
        health = max(0, health - 20 * dt)
  draw_world()
  # Multiplayer is an ongoing deathmatch: the enemy list is intentionally
  # empty, so it must not trigger the single-player victory screen.
  if not network and enemies and (health <= 0 or not enemies):
    game_over = True
    unlock_mouse()
    label = "YOU WIN!" if not enemies else "YOU DIED"
    button = "NEXT LEVEL" if not enemies else "TRY AGAIN"
    canvas.create_text(WIDTH // 2, HEIGHT // 2, fill="#ffe45c", font=("Consolas", 42, "bold"),
               text=label)
    x1, y1, x2, y2 = WIDTH // 2 - 110, HEIGHT // 2 + 45, WIDTH // 2 + 110, HEIGHT // 2 + 90
    canvas.create_rectangle(x1, y1, x2, y2, fill="#263b5c", outline="#ffe45c", width=2)
    canvas.create_text(WIDTH // 2, (y1 + y2) // 2, text=button, fill="white", font=("Consolas", 16, "bold"))
    end_button = (x1, y1, x2, y2, reset_level if not enemies else retry_level)
  else:
    # Pace frames from the actual render time instead of accumulating timer drift.
    root.after(8, tick)


root.bind("<KeyPress>", handle_key_press)
root.bind("<KeyRelease>", lambda event: keys.discard(event.keysym.lower()))
canvas.bind("<Motion>", mouse_look)
canvas.bind("<Enter>", reset_mouse_tracking)
canvas.bind("<Button-1>", fire)
canvas.bind("<Button-1>", end_button_click, add="+")
root.bind("<space>", fire)
root.bind("<F11>", toggle_fullscreen)
root.bind("<Escape>", close_game)
canvas.focus_set()
show_start_menu()
tick()
root.mainloop()


