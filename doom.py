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
try:
  import winsound
except ImportError:
  winsound = None
import urllib.request
import urllib.parse
import urllib.error
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

WIDTH, HEIGHT = 1280, 720

TILE = 64
# Wider bands greatly reduce Tk canvas objects while remaining smooth at game
# resolution.  The renderer fills each band, so there are no gaps between it.
# Larger bands substantially reduce Tk canvas work while remaining visually
# smooth at fullscreen resolution.
# Fewer, wider bands greatly reduce Tk canvas work.  Wall shading and the
# world-locked texture below keep the result smooth at fullscreen size.
RENDER_COLUMN_STEP = 8
MAP_SEED = random.SystemRandom().randint(0, 2**31 - 1)
# Keep the collision margin close to the player's visible center so walls do
# not feel like they have an oversized invisible border.
PLAYER_RADIUS = 10
ENEMY_RADIUS = 20
ENEMY_AGGRO_RANGE = 360
player = [2.5 * TILE, 2.5 * TILE]
angle = 0.0
health = 100
score = 0
level = 1
MAGAZINE_SIZE = 8
SHOTGUN_MAGAZINE_SIZE = 4
bullets = MAGAZINE_SIZE
shotgun_shells = 0
shotgun_unlocked = False
last_shotgun_shot = -0.5
weapons = ("PISTOL", "SHOTGUN")
weapon_index = 0
ENEMY_STARTS = [(8.5, 3.5), (12.5, 7.5), (5.5, 8.5)]
SUN_POSITION = (7.5 * TILE, 1.5 * TILE)
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
public_ip_cache = None
public_ip_lock = threading.Lock()


def get_local_ip():
  """Return this computer's LAN address without requiring internet access."""
  sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
  try:
    sock.connect(("8.8.8.8", 80))
    return sock.getsockname()[0]
  except OSError:
    return "127.0.0.1"
  finally:
    sock.close()


def get_public_ip():
  """Get the public IPv4 address players can use to join this host."""
  global public_ip_cache
  with public_ip_lock:
    if public_ip_cache:
      return public_ip_cache

  def lookup(url):
    try:
      request = urllib.request.Request(url, headers={"User-Agent": "HOPE"})
      with urllib.request.urlopen(request, timeout=3) as response:
        address = response.read(64).decode().strip()
      if address and all(part.isdigit() and 0 <= int(part) <= 255
                         for part in address.split(".")) and address.count(".") == 3:
        return address
    except (OSError, ValueError, urllib.error.URLError):
      return None
    return None

  # Try several providers concurrently; some networks block one or more of them.
  endpoints = (
      "https://api.ipify.org", "https://ifconfig.me/ip", "https://icanhazip.com",
      "https://ipv4.icanhazip.com", "http://checkip.amazonaws.com")
  with ThreadPoolExecutor(max_workers=len(endpoints)) as pool:
    futures = [pool.submit(lookup, endpoint) for endpoint in endpoints]
    while futures:
      for future in futures[:]:
        if future.done():
          futures.remove(future)
          try:
            address = future.result()
          except Exception:
            address = None
          if address:
            with public_ip_lock:
              public_ip_cache = address
            return address
      time.sleep(0.02)
  return None


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
    public_ip = get_public_ip()
    while network:
      # Announce the public endpoint; local discovery still uses the packet's
      # source address when a public address cannot be detected.
      host_address = public_ip or username or "Host"
      sock.sendto(f"HOPE {host_address} {port}".encode(),
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


def open_internet_port(port):
  """Try to make hosting work behind a home router via UPnP.

  Direct TCP connections cannot cross NAT by themselves.  This standard
  UPnP request asks the local router to forward the game port; if the router
  does not support UPnP, hosting still works normally with manual forwarding.
  """
  message = ("M-SEARCH * HTTP/1.1\r\n"
             "HOST: 239.255.255.250:1900\r\n"
             "MAN: \"ssdp:discover\"\r\n"
             "MX: 1\r\n"
             "ST: urn:schemas-upnp-org:device:InternetGatewayDevice:1\r\n\r\n")
  sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
  sock.settimeout(1)
  try:
    sock.sendto(message.encode(), ("239.255.255.250", 1900))
    response, _ = sock.recvfrom(4096)
    location = next((line.split(":", 1)[1].strip()
                     for line in response.decode(errors="ignore").splitlines()
                     if line.lower().startswith("location:")), None)
    if not location:
      return None
    description = urllib.request.urlopen(location, timeout=1.5).read()
    root_xml = ET.fromstring(description)
    namespace = {"u": "urn:schemas-upnp-org:device-1-0"}
    service = next((item for item in root_xml.findall(".//u:service", namespace)
                    if "WANIPConnection" in (item.findtext("u:serviceType", "", namespace))), None)
    if service is None:
      return None
    control = urllib.parse.urljoin(location, service.findtext("u:controlURL", "", namespace))
    # A UDP socket used for SSDP commonly reports 0.0.0.0 here.  Supplying
    # that address to the router makes the mapping unusable from the
    # internet, so resolve the actual LAN address separately.
    local_ip = get_local_ip()
    body = ("<?xml version=\"1.0\"?>"
            "<s:Envelope xmlns:s=\"http://schemas.xmlsoap.org/soap/envelope/\" "
            "s:encodingStyle=\"http://schemas.xmlsoap.org/soap/encoding/\"><s:Body>"
            "<u:AddPortMapping xmlns:u=\"urn:schemas-upnp-org:service:WANIPConnection:1\">"
            f"<NewRemoteHost></NewRemoteHost><NewExternalPort>{port}</NewExternalPort>"
            "<NewProtocol>TCP</NewProtocol>"
            f"<NewInternalPort>{port}</NewInternalPort><NewInternalClient>{local_ip}</NewInternalClient>"
            "<NewEnabled>1</NewEnabled><NewPortMappingDescription>HOPE</NewPortMappingDescription>"
            "<NewLeaseDuration>0</NewLeaseDuration></u:AddPortMapping></s:Body></s:Envelope>")
    request = urllib.request.Request(control, data=body.encode(), method="POST",
      headers={"Content-Type": "text/xml; charset=\"utf-8\"",
               "SOAPAction": '"urn:schemas-upnp-org:service:WANIPConnection:1#AddPortMapping"'})
    urllib.request.urlopen(request, timeout=1.5).read()
    return local_ip
  except (OSError, ValueError, urllib.error.URLError, ET.ParseError):
    return None
  finally:
    sock.close()


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
            # Carry the shooter's health with the shot.  A shot can remain
            # queued while a death position packet is in flight; validating
            # only the last replicated position would let a dead player fire.
            shooter_packet_health = float(parts[4]) if len(parts) > 4 else 100.0
          except ValueError:
            continue
          # The sender's latest position is used to validate a shot locally.
          with network_lock:
            shooter = remote_players.get(parts[1])
            shooter_health = remote_health.get(parts[1], 100)
          # A dead player must not be able to damage anyone, even if a shot
          # packet was already queued when the player died.
          if shooter_health <= 0 or shooter_packet_health <= 0:
            continue
          if shooter is not None:
            dx, dy = player[0] - shooter[0], player[1] - shooter[1]
            distance = math.hypot(dx, dy)
            difference = abs((math.atan2(dy, dx) - shooter_angle + math.pi) %
                             (2 * math.pi) - math.pi)
            if visible(player[0], player[1]) and difference < (0.45 if weapon else 0.08):
              damage = 12 if not weapon else max(5, int(50 * max(0.0, 1 - distance / 500)))
              was_alive = health > 0
              if was_alive:
                add_damage_indicator(shooter[0], shooter[1])
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
      network_status = "MULTIPLAYER"
      threading.Thread(target=network_loop, args=(sock,), daemon=True).start()
    except OSError:
      break


def start_network():
  """Start LAN mode: `--host [port]` or `--join host [port]`."""
  global network, network_status, enemies, network_peers
  try:
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode not in ("--host", "--join"):
      raise ValueError("missing multiplayer mode")
    port = int(sys.argv[3] if mode == "--join" and len(sys.argv) > 3 else
               sys.argv[2] if mode == "--host" and len(sys.argv) > 2 else 4711)
    if mode == "--host":
      public_ip = get_public_ip()
      endpoint = f"{public_ip}:{port}" if public_ip else f"<public-ip>:{port}"
      local_endpoint = f"{get_local_ip()}:{port}"
      print(f"Hosting multiplayer at {endpoint} (LAN: {local_endpoint}).")
      print("Give the public address to internet players; allow TCP this port in the firewall.")
      listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
      listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
      listener.bind(("0.0.0.0", port))
      listener.listen(16)
      # Keep the listener alive and accept clients in the background.  The
      # previous blocking accept prevented the game from starting until a
      # local client connected, and made hosting over the internet awkward.
      network = listener
      network_status = "WAITING FOR PLAYER"
      def configure_router():
        mapped_ip = open_internet_port(port)
        if mapped_ip:
          print(f"Router port forwarding enabled for {mapped_ip}:{port}.")
        else:
          print("Automatic port forwarding unavailable; forward TCP port "
                f"{port} to {get_local_ip()} manually if needed.")
      threading.Thread(target=configure_router, daemon=True).start()
      threading.Thread(target=accept_late_players, args=(listener,), daemon=True).start()
      threading.Thread(target=announce_host, args=(port,), daemon=True).start()
    elif mode == "--join" and len(sys.argv) > 2:
      host = sys.argv[2].strip()
      if not host:
        raise ValueError("missing host address")
      print(f"Joining multiplayer host {host} on port {port}...")
      client = socket.create_connection((host, port), timeout=10)
      client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
      client.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
      network = client
      print("Connected to host. Starting game.")
    if network:
      enemies = []
      network_status = "CONNECTING"
      # The host's listening socket is not a connected peer and cannot carry
      # gameplay packets.  Keeping it in this list prevented the host from
      # sending its state to accepted clients, so joining players only saw
      # their own local view.  Accepted sockets are added by
      # accept_late_players(); clients still register their connected socket.
      if mode != "--host":
        with network_peers_lock:
          if network not in network_peers:
            network_peers.append(network)
      # The host owns the seed; the joining client replaces its local seed
      # when this packet is received by network_loop().
      if mode == "--host":
        # Accepted clients receive the map seed in accept_late_players().
        pass
      else:
        threading.Thread(target=network_loop, args=(network,), daemon=True).start()
  except (OSError, ValueError, IndexError, socket.timeout) as error:
    print(f"Failed to start multiplayer ({error}); continuing in singleplayer mode.")
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
  if network and health > 0:
    try:
      packet = f"S {PLAYER_ID} {angle:.3f} {weapon_index} {health:.1f}\n".encode()
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
  # Keep a small area around the initial spawn clear as well.  This prevents
  # the player's circular hitbox from touching a wall immediately after a
  # level starts.
  protected = {(x, y) for x in range(1, 4) for y in range(1, 4)}
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

  # Random walls can otherwise create isolated rooms.  Join every floor
  # region to the starting region by carving a simple route through walls.
  # This preserves the random layout while guaranteeing that every part of
  # the map is reachable.
  def reachable():
    seen = {(2, 2)}
    pending = [(2, 2)]
    while pending:
      x, y = pending.pop()
      for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
        if (0 <= nx < width and 0 <= ny < height and
            rows[ny][nx] == "." and (nx, ny) not in seen):
          seen.add((nx, ny))
          pending.append((nx, ny))
    return seen

  connected = reachable()
  while True:
    isolated = [(x, y) for y, row in enumerate(rows) for x, cell in enumerate(row)
                if cell == "." and (x, y) not in connected]
    if not isolated:
      break
    target = min(isolated, key=lambda cell: min(
        abs(cell[0] - x) + abs(cell[1] - y) for x, y in connected))
    x, y = target
    while (x, y) not in connected:
      rows[y] = rows[y][:x] + "." + rows[y][x + 1:]
      candidates = [(nx, ny) for nx, ny in
                    ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1))
                    if 0 <= nx < width and 0 <= ny < height]
      x, y = min(candidates, key=lambda cell: min(
          abs(cell[0] - cx) + abs(cell[1] - cy) for cx, cy in connected))
    connected = reachable()
  return rows


def create_singleplayer_enemies():
  """Create the original enemies plus one extra enemy per level."""
  enemies_for_level = [[x * TILE, y * TILE, 2] for x, y in ENEMY_STARTS]
  occupied = {(int(x), int(y)) for x, y in ENEMY_STARTS}
  candidates = [(x, y) for y, row in enumerate(MAP) for x, cell in enumerate(row)
                if cell == "." and (x, y) not in occupied
                and (x, y) not in {(2, 2), (3, 2), (2, 3)}]
  randomizer = random.Random(MAP_SEED + level)
  randomizer.shuffle(candidates)
  for x, y in candidates[:max(0, level - 1)]:
    enemies_for_level.append([x * TILE + TILE / 2, y * TILE + TILE / 2, 2])
  return enemies_for_level


MAP = make_map()
enemies = create_singleplayer_enemies()
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
damage_indicators = []
enemy_projectiles = []
enemy_shot_times = {}
last_damage_indicator_time = 0.0
pickup_bob = 0.0
gunshot_audio = None
last_tick_time = time.perf_counter()
strafe_velocity = 0.0
root = tk.Tk()

root.title("HOPE: The Python Experiment")
fullscreen = True
mouse_sensitivity = 0.00045
settings_window = None
root.attributes("-fullscreen", fullscreen)
root.update_idletasks()
WIDTH, HEIGHT = root.winfo_screenwidth(), root.winfo_screenheight()

root.resizable(True, True)
canvas = tk.Canvas(root, width=WIDTH, height=HEIGHT, highlightthickness=0)
canvas.pack(fill="both", expand=True)

# Keep the sun as one cached image rather than redrawing its outline every
# frame.  This also prevents the canvas from accumulating thin edge artifacts.
sun_image = tk.PhotoImage(width=145, height=145)
for sun_y in range(145):
  pixels = []
  for sun_x in range(145):
    distance = math.hypot(sun_x - 72, sun_y - 72)
    if distance <= 72:
      pixels.append("#%02x%02x%02x" % (max(0, int(255 - distance * 1.2)),
                                      max(0, int(190 - distance * 0.8)), 55))
    else:
      pixels.append("#000000")
  sun_image.put("{" + " ".join(pixels) + "}", to=(0, sun_y))
  for sun_x in range(145):
    if math.hypot(sun_x - 72, sun_y - 72) > 72:
      sun_image.transparency_set(sun_x, sun_y, True)


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
  username_entry.insert(0, f"Player{random.SystemRandom().randint(1000, 9999)}")
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

  def autofill_host_connection():
    """Fill host connection details with this computer's public address."""
    mode.set("host")
    port_entry.delete(0, tk.END)
    port_entry.insert(0, "4711")
    multiplayer_button.config(state="disabled", text="Finding public IP...")
    lookup_result = [None, False]

    def find_public_ip():
      # Tkinter widgets must only be accessed from the main thread.  Store
      # the result here; the main thread polls it below and updates the UI.
      lookup_result[0] = get_public_ip()
      lookup_result[1] = True

    def update_fields():
      if not menu.winfo_exists():
        return
      if not lookup_result[1]:
        menu.after(100, update_fields)
        return
      public_ip = lookup_result[0]
      host_entry.delete(0, tk.END)
      host_entry.insert(0, public_ip or "Your public IP could not be found")
      multiplayer_button.config(state="normal", text="Start Multiplayer")
      quick_join_status.config(
          text=("Give your friend the IP above and port 4711. "
                "Allow TCP 4711 through your firewall/router."
                if public_ip else
                "Could not find your public IP; enter it manually."),
          fg="#176b2c" if public_ip else "#b00020")

    threading.Thread(target=find_public_ip, daemon=True).start()
    menu.after(100, update_fields)

  tk.Button(menu, text="Auto-fill Host IP + Port", width=30,
            command=autofill_host_connection).pack(pady=2)
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


def show_settings():
  """Open the settings window without leaving the current game."""
  global settings_window, mouse_sensitivity
  if settings_window is not None and settings_window.winfo_exists():
    settings_window.focus_force()
    return
  unlock_mouse()
  settings_window = tk.Toplevel(root)
  settings_window.title("HOPE SETTINGS")
  settings_window.geometry("360x245")
  settings_window.resizable(False, False)
  settings_window.transient(root)
  settings_window.grab_set()
  settings_window.focus_force()
  tk.Label(settings_window, text="Settings", font=("Consolas", 18, "bold")).pack(pady=12)

  fullscreen_var = tk.BooleanVar(value=fullscreen)
  tk.Checkbutton(settings_window, text="Fullscreen", variable=fullscreen_var,
                 command=lambda: (set_fullscreen(fullscreen_var.get()))).pack()
  tk.Label(settings_window, text="Mouse sensitivity").pack(pady=(10, 0))
  sensitivity = tk.DoubleVar(value=mouse_sensitivity * 100000)
  tk.Scale(settings_window, from_=10, to=100, orient="horizontal", length=250,
           variable=sensitivity, command=lambda value: set_mouse_sensitivity(value)).pack()

  def close_settings():
    global settings_window
    if settings_window is not None and settings_window.winfo_exists():
      settings_window.grab_release()
      settings_window.destroy()
    settings_window = None
    if not game_over:
      lock_mouse()

  tk.Button(settings_window, text="Close", width=18, command=close_settings).pack(pady=12)
  settings_window.protocol("WM_DELETE_WINDOW", close_settings)


def set_fullscreen(enabled):
  global fullscreen
  fullscreen = bool(enabled)
  root.attributes("-fullscreen", fullscreen)


def set_mouse_sensitivity(value):
  global mouse_sensitivity
  mouse_sensitivity = max(0.0001, float(value) / 100000)


def blocked(x, y):
    gx, gy = int(x // TILE), int(y // TILE)
    return gy < 0 or gy >= len(MAP) or gx < 0 or gx >= len(MAP[0]) or MAP[gy][gx] == "1"


def circle_hits_wall(x, y, radius):
  """Return whether a circle overlaps the rectangular map wall cells."""
  # Include every cell touched by the circle.  Using the circle bounds here
  # keeps collision coordinates aligned with the same TILE boundaries used by
  # blocked() and cast_wall_ray().
  left = max(0, math.floor((x - radius) / TILE))
  right = min(len(MAP[0]) - 1, math.floor((x + radius) / TILE))
  top = max(0, math.floor((y - radius) / TILE))
  bottom = min(len(MAP) - 1, math.floor((y + radius) / TILE))
  for gy in range(top, bottom + 1):
    for gx in range(left, right + 1):
      if MAP[gy][gx] != "1":
        continue
      nearest_x = max(gx * TILE, min(x, (gx + 1) * TILE))
      nearest_y = max(gy * TILE, min(y, (gy + 1) * TILE))
      if math.hypot(x - nearest_x, y - nearest_y) <= radius:
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


def add_damage_indicator(source_x, source_y):
  """Queue a brief indicator pointing from the attacker toward the player."""
  global last_damage_indicator_time
  now = time.perf_counter()
  if now - last_damage_indicator_time < 0.12:
    return
  last_damage_indicator_time = now
  # Point toward the attacker from the player's position, not back toward
  # the player.  This keeps the indicator aligned with the enemy on screen.
  direction = math.atan2(source_y - player[1], source_x - player[0])
  damage_indicators.append([direction, 0.7])


def draw_damage_indicators():
  """Draw red arrows around the screen edge toward incoming damage."""
  center_x, center_y = WIDTH / 2, HEIGHT / 2
  radius = min(WIDTH, HEIGHT) * 0.38
  for direction, remaining in damage_indicators:
    relative = (direction - angle + math.pi) % (2 * math.pi) - math.pi
    # Map the enemy's bearing into screen space: forward is up and right is
    # right, so the indicator points to the attacker's actual direction.
    direction_x, direction_y = math.sin(relative), -math.cos(relative)
    x = center_x + direction_x * radius
    y = center_y + direction_y * radius
    tip = (x + direction_x * 38, y + direction_y * 38)
    side = math.atan2(direction_y, direction_x) + math.pi / 2
    left = (x + math.cos(side) * 18, y + math.sin(side) * 18)
    right = (x - math.cos(side) * 18, y - math.sin(side) * 18)
    intensity = max(0.35, remaining / 0.7)
    red = int(255 * intensity)
    color = "#%02x2020" % red
    canvas.create_polygon(tip, left, right, fill=color,
                outline="#ffb0b0", width=3)
    # Keep the warning readable even when the attacker is off-screen.
    canvas.create_text(x, y - 26, text="DAMAGE", fill="#ff7070",
               font=("Consolas", 11, "bold"))


def move(dx, dy):
  """Move with swept substeps so movement cannot pass through a wall."""
  distance = math.hypot(dx, dy)
  steps = max(1, math.ceil(distance / max(1, PLAYER_RADIUS / 2)))
  step_x, step_y = dx / steps, dy / steps
  for _ in range(steps):
    if not circle_hits_wall(player[0] + step_x, player[1], PLAYER_RADIUS):
      player[0] += step_x
    if not circle_hits_wall(player[0], player[1] + step_y, PLAYER_RADIUS):
      player[1] += step_y


def shoot():
  global score, bullets, shotgun_shells, last_shotgun_shot
  if health <= 0:
    return
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


def update_enemies(dt):
  """Move enemies slowly and let them fire visible, dodgeable projectiles."""
  now = time.perf_counter()
  for enemy in enemies:
    ex, ey = enemy[0], enemy[1]
    distance = math.hypot(player[0] - ex, player[1] - ey)
    # Enemies stay put until the player enters their engagement range, then
    # approach while keeping a little space from the player.
    if health > 0 and 115 < distance <= ENEMY_AGGRO_RANGE:
      move_x = (player[0] - ex) / max(distance, 1) * 24 * dt
      move_y = (player[1] - ey) / max(distance, 1) * 24 * dt
      if not circle_hits_wall(ex + move_x, ey, ENEMY_RADIUS):
        enemy[0] += move_x
      if not circle_hits_wall(enemy[0], ey + move_y, ENEMY_RADIUS):
        enemy[1] += move_y

    last_shot = enemy_shot_times.get(id(enemy), 0.0)
    if health > 0 and distance < 520 and now - last_shot >= 2.2 and visible(ex, ey):
      direction = math.atan2(player[1] - ey, player[0] - ex)
      enemy_projectiles.append([ex, ey, math.cos(direction) * 125,
                                math.sin(direction) * 125, 0.0])
      enemy_shot_times[id(enemy)] = now

  for projectile in enemy_projectiles[:]:
    projectile[0] += projectile[2] * dt
    projectile[1] += projectile[3] * dt
    projectile[4] += dt
    if (projectile[4] > 5 or blocked(projectile[0], projectile[1]) or
        math.hypot(projectile[0] - player[0], projectile[1] - player[1]) < PLAYER_RADIUS + 7):
      if math.hypot(projectile[0] - player[0], projectile[1] - player[1]) < PLAYER_RADIUS + 7 and health > 0:
        add_damage_indicator(projectile[0], projectile[1])
        globals()["health"] = max(0, health - 18)
      enemy_projectiles.remove(projectile)


def play_gun_sound(weapon):
  """Play a short synthesized firing sound without blocking the game loop."""
  def play():
    if winsound is None:
      try:
        root.bell()
      except tk.TclError:
        pass
      return
    try:
      if weapon:
        winsound.Beep(95, 90)
        winsound.Beep(65, 75)
      else:
        winsound.Beep(180, 45)
        winsound.Beep(120, 35)
    except (RuntimeError, OSError):
      pass
  threading.Thread(target=play, daemon=True).start()


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
      angle += delta * mouse_sensitivity
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
  if game_over or health <= 0:
    return
  ammo_before = (bullets, shotgun_shells)
  shoot()
  if (bullets, shotgun_shells) != ammo_before:
    muzzle_flash = 0.12
    play_gun_sound(weapon_index)
    send_network_shot()


def handle_key_press(event):
  key = event.keysym.lower()
  keys.add(key)
  if key == "t" and not (settings_window is not None and settings_window.winfo_exists()):
    show_settings()
  elif key == "r":
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
  global player, angle, health, score, level, bullets, shotgun_shells, respawn_at
  global MAP, MAP_SEED, pickup_cells
  global shotgun_unlocked, weapon_index, enemies, shotgun_pickup, game_over, last_shotgun_shot
  global enemy_projectiles, enemy_shot_times
  if not network:
    # Use a fresh seed for each single-player restart so the level changes.
    level += 1
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
  enemies = [] if network else create_singleplayer_enemies()
  enemy_projectiles = []
  enemy_shot_times = {}
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
  # Layered gradients make the ceiling and floor feel deeper than two flat
  # colors while keeping the canvas object count modest.
  ceiling_colors = ("#10172b", "#141c33", "#18213c", "#202945")
  floor_colors = ("#261e2b", "#302331", "#3f2d38", "#51333a")
  band_height = max(1, horizon // len(ceiling_colors))
  for index, color in enumerate(ceiling_colors):
    canvas.create_rectangle(0, index * band_height, WIDTH,
                            (index + 1) * band_height + 1,
                            fill=color, outline="")
  band_height = max(1, (HEIGHT - horizon) // len(floor_colors))
  for index, color in enumerate(floor_colors):
    canvas.create_rectangle(0, horizon + index * band_height, WIDTH,
                            horizon + (index + 1) * band_height + 1,
                            fill=color, outline="")
  # Stars, distant haze, and overhead light shafts give the map depth.
  for index in range(24):
    x = (index * 347 + MAP_SEED % 271) % max(1, WIDTH)
    y = 25 + (index * 53 + MAP_SEED % 97) % max(30, horizon - 45)
    size = 1 + index % 2
    canvas.create_oval(x, y, x + size, y + size, fill="#7786ad", outline="")
  for x in range(-WIDTH, WIDTH * 2, 220):
    canvas.create_polygon(x, horizon - 35, x + 70, horizon - 35,
                          x + 260, HEIGHT, x + 80, HEIGHT,
                          fill="#3b2935", outline="")
  # Perspective floor seams add scale and depth to the otherwise flat floor.
  # They are deliberately sparse so the raycaster remains responsive.
  for row in range(1, 9):
    depth = row / 8
    y = horizon + int((HEIGHT - horizon) * depth * depth)
    canvas.create_line(0, y, WIDTH, y, fill="#68434a", width=1)
  for column in range(-8, 9):
    bottom_x = WIDTH / 2 + column * WIDTH / 8
    canvas.create_line(WIDTH / 2, horizon, bottom_x, HEIGHT,
                       fill="#49313b", width=1)
  # A subtle ceiling vanishing-point pattern balances the floor detail.
  for row in range(1, 5):
    depth = row / 5
    y = horizon - int(horizon * depth * depth)
    canvas.create_line(0, y, WIDTH, y, fill="#293452", width=1)


def draw_sun(horizon, view_angle, fov, origin_x, origin_y):
  """Draw the sun from a fixed world location, independent of the cursor."""
  dx, dy = SUN_POSITION[0] - origin_x, SUN_POSITION[1] - origin_y
  distance = math.hypot(dx, dy)
  relative = (math.atan2(dy, dx) - view_angle + math.pi) % (2 * math.pi) - math.pi
  if distance <= 1 or abs(relative) >= fov / 2:
    return
  x = int(WIDTH / 2 + math.tan(relative) * WIDTH / (2 * math.tan(fov / 2)))
  y = horizon - max(80, min(220, int(16000 / distance)))
  canvas.create_image(x, y, image=sun_image, anchor="center")


def cast_wall_ray(origin_x, origin_y, ray_angle, max_distance=800):
  """Trace a ray through the map grid using a stable DDA traversal."""
  direction_x, direction_y = math.cos(ray_angle), math.sin(ray_angle)
  cell_x, cell_y = int(origin_x // TILE), int(origin_y // TILE)
  step_x = 1 if direction_x >= 0 else -1
  step_y = 1 if direction_y >= 0 else -1
  delta_x = abs(TILE / direction_x) if abs(direction_x) > 1e-12 else float("inf")
  delta_y = abs(TILE / direction_y) if abs(direction_y) > 1e-12 else float("inf")
  next_x = (((cell_x + 1) * TILE - origin_x) / direction_x
            if direction_x > 0 else
            ((cell_x * TILE - origin_x) / direction_x
             if direction_x < 0 else float("inf")))
  next_y = (((cell_y + 1) * TILE - origin_y) / direction_y
            if direction_y > 0 else
            ((cell_y * TILE - origin_y) / direction_y
             if direction_y < 0 else float("inf")))
  distance = 0.0
  while distance <= max_distance:
    if next_x < next_y:
      cell_x += step_x
      distance, next_x = next_x, next_x + delta_x
    else:
      cell_y += step_y
      distance, next_y = next_y, next_y + delta_y
    if (cell_y < 0 or cell_y >= len(MAP) or cell_x < 0 or
        cell_x >= len(MAP[0]) or MAP[cell_y][cell_x] == "1"):
      distance = max(0.001, min(distance, max_distance))
      return (distance, origin_x + direction_x * distance,
              origin_y + direction_y * distance)
  return (max_distance, origin_x + direction_x * max_distance,
          origin_y + direction_y * max_distance)


def draw_vignette():
  """Keep the center of the view clean; aiming is intentionally unobstructed."""
  # Tk Canvas has no alpha compositing for normal shapes, so use progressively
  # darker edge bands instead of a translucent overlay.  The clear center
  # preserves visibility while making the scene feel substantially richer.
  edge = max(18, min(WIDTH, HEIGHT) // 12)
  for index in range(5):
    inset = index * edge // 5
    shade = 18 + index * 5
    color = "#%02x%02x%02x" % (shade // 2, shade // 2, shade)
    canvas.create_rectangle(inset, inset, WIDTH - inset, HEIGHT - inset,
                            outline=color, width=max(2, edge // 5))



def draw_world():
  global end_button
  end_button = None
  canvas.delete("all")
  # Number keys roll the camera left or right.
  roll = 0.06 if "1" in keys else -0.06 if "2" in keys else 0
  view_angle = angle - 0.04 if "1" in keys else angle + 0.04 if "2" in keys else angle
  horizon = HEIGHT // 2
  # Use a wide, standard 90-degree horizontal field of view.  Keep the
  # projection scale tied to the FOV so walls retain their correct size.
  fov = math.radians(90)
  origin_x, origin_y = view_origin()
  draw_graphics_backdrop(horizon)
  draw_sun(horizon, view_angle, fov, origin_x, origin_y)
  # Render bands instead of one canvas object per pixel column.
  for column in range(0, WIDTH, RENDER_COLUMN_STEP):
    column_horizon = horizon + int(roll * (column - WIDTH / 2))
    ray_angle = view_angle - fov / 2 + fov * (column + RENDER_COLUMN_STEP / 2) / WIDTH
    distance, hit_x, hit_y = cast_wall_ray(origin_x, origin_y, ray_angle)
    distance *= math.cos(ray_angle - view_angle)
    wall_height = min(HEIGHT, int(46080 / max(distance, 1)))
    # Light the wall according to which face the ray strikes.  This gives
    # corridors readable shape instead of making every wall equally bright.
    face_light = 1.0 if abs(math.cos(ray_angle)) > abs(math.sin(ray_angle)) else 0.78
    shade = max(22, min(220, int(24500 / max(distance, 1) * face_light)))
    # Use smooth distance lighting instead of artificial wall bands or stripes.
    warm = min(255, shade + 12)
    color = "#%02x%02x%02x" % (warm, int(shade * 0.56), int(shade * 0.38))
    canvas.create_rectangle(column, column_horizon - wall_height // 2,
      min(WIDTH, column + RENDER_COLUMN_STEP),
      column_horizon + wall_height // 2, fill=color, outline="")

    # Procedural masonry texture: the hit position makes the pattern stay
    # attached to the world while the player turns, instead of swimming with
    # the screen.  Multiple scales of warm stone, mortar, and cracks give the
    # walls considerably more depth without requiring external image assets.
    tile_x, tile_y = int(hit_x // TILE), int(hit_y // TILE)
    wall_u = int((hit_x if abs(math.cos(ray_angle)) > abs(math.sin(ray_angle))
                  else hit_y) % TILE)
    brick_row = int(distance // 22)
    seed = (tile_x * 928371 + tile_y * 364479 + brick_row * 193939 + wall_u // 8) & 255
    texture_shade = max(12, min(230, shade + (seed % 25) - 12))
    texture_color = "#%02x%02x%02x" % (min(255, texture_shade + 16),
                                        int(texture_shade * 0.60),
                                        int(texture_shade * 0.43))
    # One textured fill per band is considerably faster than drawing several
    # mortar objects for every ray, while the deterministic variation keeps
    # the masonry tied to the world instead of the screen.
    canvas.create_rectangle(column, column_horizon - wall_height // 2 + 1,
      min(WIDTH, column + RENDER_COLUMN_STEP),
      column_horizon + wall_height // 2 - 1, fill=texture_color, outline="")
    # A single inset highlight gives the stone a bevel without two extra
    # canvas objects for every ray.
    highlight = "#%02x%02x%02x" % (min(255, texture_shade + 28),
                                   min(220, int(texture_shade * 0.70)),
                                   min(180, int(texture_shade * 0.50)))
    canvas.create_line(column, column_horizon - wall_height // 2 + 1,
                       min(WIDTH, column + RENDER_COLUMN_STEP),
                       column_horizon - wall_height // 2 + 1,
                       fill=highlight, width=1)

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

  # Enemy fireballs are drawn as bright world-space projectiles.
  for projectile_x, projectile_y, _, _, _ in enemy_projectiles:
    dx, dy = projectile_x - origin_x, projectile_y - origin_y
    distance = math.hypot(dx, dy)
    relative = (math.atan2(dy, dx) - view_angle + math.pi) % (2 * math.pi) - math.pi
    if distance > 1 and abs(relative) < fov / 2 and visible(projectile_x, projectile_y):
      x = int(WIDTH / 2 + math.tan(relative) * WIDTH / (2 * math.tan(fov / 2)))
      size = max(4, int(2200 / distance))
      y = horizon
      canvas.create_oval(x - size, y - size, x + size, y + size,
                         fill="#ff5722", outline="#ffe082", width=2)

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

  # Keep aiming clear and consistent, regardless of the weapon or scene.
  crosshair_x, crosshair_y = WIDTH // 2, HEIGHT // 2
  crosshair_color = "#fff4a3" if muzzle_flash > 0 else "#ffffff"
  canvas.create_line(crosshair_x - 12, crosshair_y, crosshair_x - 4, crosshair_y,
                     fill=crosshair_color, width=2)
  canvas.create_line(crosshair_x + 4, crosshair_y, crosshair_x + 12, crosshair_y,
                     fill=crosshair_color, width=2)
  canvas.create_line(crosshair_x, crosshair_y - 12, crosshair_x, crosshair_y - 4,
                     fill=crosshair_color, width=2)
  canvas.create_line(crosshair_x, crosshair_y + 4, crosshair_x, crosshair_y + 12,
                     fill=crosshair_color, width=2)
  canvas.create_oval(crosshair_x - 2, crosshair_y - 2, crosshair_x + 2,
                     crosshair_y + 2, fill=crosshair_color, outline="")

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
             text=f"LEVEL {level}   SCORE {score:04d}   {weapons[weapon_index]}")
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
  damage_indicators[:] = [[direction, remaining - dt]
                          for direction, remaining in damage_indicators
                          if remaining > dt]
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
  target_strafe = (("d" in keys) - ("a" in keys)) * 110 if health > 0 else 0
  strafe_velocity += (target_strafe - strafe_velocity) * (1 - math.exp(-12 * dt))
  strafe = strafe_velocity * dt
  if health > 0:
    move(math.cos(angle) * forward + math.cos(angle + math.pi / 2) * strafe,
       math.sin(angle) * forward + math.sin(angle + math.pi / 2) * strafe)
  if not network and health > 0:
    update_enemies(dt)
  send_network_state()
  if health > 0 and shotgun_pickup is not None and math.hypot(shotgun_pickup[0] - player[0], shotgun_pickup[1] - player[1]) < 32:
    shotgun_pickup = None
    shotgun_unlocked = True
    shotgun_shells = SHOTGUN_MAGAZINE_SIZE
    switch_weapon(1)
  for enemy in enemies:
    if math.hypot(enemy[0] - player[0], enemy[1] - player[1]) < PLAYER_RADIUS + ENEMY_RADIUS + 18:
      if health > 0:
        add_damage_indicator(enemy[0], enemy[1])
        health = max(0, health - 20 * dt)
  draw_world()
  draw_damage_indicators()
  # Multiplayer is an ongoing deathmatch: the enemy list is intentionally
  # empty, so it must not trigger the single-player victory screen.
  if not network and (health <= 0 or not enemies):
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
    # Avoid queuing frames faster than Tk can render them; this prevents input
    # lag and timer buildup on slower machines.
    # Keep a steady frame cadence without flooding Tk's event queue.
    root.after(16, tick)


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


