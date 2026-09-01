import os
import io
import json
import gzip
import zlib
import shutil
import tempfile
import zipfile
from pathlib import Path
from flask import Flask, render_template, request, jsonify, send_file
import nbtlib

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024
app.config['JSON_AS_ASCII'] = False

sessions = {}
ITEMS_DB = None
ENCHANTMENTS_DB = None

def load_data():
    global ITEMS_DB, ENCHANTMENTS_DB
    base = Path(__file__).parent / 'data'
    with open(base / 'items.json', 'r', encoding='utf-8') as f:
        ITEMS_DB = json.load(f)
    with open(base / 'enchantments.json', 'r', encoding='utf-8') as f:
        ENCHANTMENTS_DB = json.load(f)

def load_nbt_data(raw_bytes):
    """
    Robustly parses NBT data in all Bedrock and Java variations:
    1. Gzip compressed (Java or compressed Bedrock chunks)
    2. Zlib compressed
    3. Bedrock level.dat with 8-byte header (4-byte version + 4-byte length)
    4. Little-Endian Bedrock NBT (standard for LevelDB player data)
    5. Raw Little-Endian Compound (without root tag header)
    6. Big-Endian Java NBT fallback
    """
    if not raw_bytes:
        raise ValueError("Empty NBT byte stream.")

    # 1. Gzip compressed
    if raw_bytes.startswith(b'\x1f\x8b'):
        try:
            decompressed = gzip.decompress(raw_bytes)
            try:
                return nbtlib.File.parse(io.BytesIO(decompressed), byteorder='little')
            except Exception:
                return nbtlib.File.parse(io.BytesIO(decompressed), byteorder='big')
        except Exception:
            pass

    # 2. Zlib compressed
    if raw_bytes.startswith((b'\x78\x9c', b'\x78\x01', b'\x78\xda')):
        try:
            decompressed = zlib.decompress(raw_bytes)
            try:
                return nbtlib.File.parse(io.BytesIO(decompressed), byteorder='little')
            except Exception:
                return nbtlib.File.parse(io.BytesIO(decompressed), byteorder='big')
        except Exception:
            pass

    # 3. Bedrock level.dat with 8-byte header
    if len(raw_bytes) > 8 and raw_bytes[8] == 0x0A:
        try:
            return nbtlib.File.parse(io.BytesIO(raw_bytes[8:]), byteorder='little')
        except Exception:
            pass

    # 4. Standard Little-Endian NBT with root tag Compound (0x0A)
    if raw_bytes.startswith(b'\x0a'):
        try:
            return nbtlib.File.parse(io.BytesIO(raw_bytes), byteorder='little')
        except Exception:
            pass

    # 5. Raw Little-Endian Compound without root tag header
    try:
        compound = nbtlib.Compound.parse(io.BytesIO(raw_bytes), byteorder='little')
        return nbtlib.File(compound)
    except Exception:
        pass

    # 6. Standard Big-Endian NBT fallback
    if raw_bytes.startswith(b'\x0a'):
        try:
            return nbtlib.File.parse(io.BytesIO(raw_bytes), byteorder='big')
        except Exception:
            pass

    # Direct fallback attempt
    try:
        return nbtlib.File.parse(io.BytesIO(raw_bytes), byteorder='little')
    except Exception as e:
        raise ValueError(f"Could not parse NBT data: {str(e)}")

def save_bedrock_nbt(nbt_file):
    buf = io.BytesIO()
    nbt_file.write(buf, byteorder="little")
    return buf.getvalue()

def py_to_nbt(val, hint_key=None):
    """
    Recursively converts Python primitives / dicts / lists into valid, typed nbtlib objects.
    """
    if isinstance(val, (nbtlib.Compound, nbtlib.List, nbtlib.String, nbtlib.Byte, 
                        nbtlib.Short, nbtlib.Int, nbtlib.Long, nbtlib.Float, 
                        nbtlib.Double, nbtlib.ByteArray, nbtlib.IntArray)):
        return val
    if isinstance(val, dict):
        return nbtlib.Compound({k: py_to_nbt(v, k) for k, v in val.items()})
    if isinstance(val, list):
        if not val:
            return nbtlib.List[nbtlib.Compound]()
        converted = [py_to_nbt(x) for x in val]
        first_type = type(converted[0])
        return nbtlib.List[first_type](converted)
    if isinstance(val, str):
        return nbtlib.String(val)
    if isinstance(val, bool):
        return nbtlib.Byte(1 if val else 0)
    if isinstance(val, int):
        if hint_key in ('Count', 'Slot', 'WasPickedUp'):
            return nbtlib.Byte(val)
        if hint_key in ('Damage', 'id', 'lvl', 'Aux'):
            return nbtlib.Short(val)
        if hint_key in ('RepairCost', 'version', 'map_scale'):
            return nbtlib.Int(val)
        return nbtlib.Int(val)
    if isinstance(val, float):
        return nbtlib.Float(val)
    return nbtlib.String(str(val))

def nbt_item_to_dict(item_tag):
    name = str(item_tag.get("Name", nbtlib.String("")))
    if not name or name == "" or name == "minecraft:air":
        return None

    result = {
        "name": name,
        "count": int(item_tag.get("Count", nbtlib.Byte(1))),
        "slot": int(item_tag.get("Slot", nbtlib.Byte(0))),
        "damage": int(item_tag.get("Damage", nbtlib.Short(0))),
        "enchantments": [],
        "customName": "",
        "_raw": dict(item_tag)
    }

    extra = item_tag.get("tag")
    if extra is not None and isinstance(extra, nbtlib.Compound):
        ench_list = extra.get("ench")
        if ench_list is not None and isinstance(ench_list, nbtlib.List):
            for ench in ench_list:
                eid = int(ench.get("id", nbtlib.Short(0)))
                elvl = int(ench.get("lvl", nbtlib.Short(1)))
                result["enchantments"].append({"id": eid, "lvl": elvl})

        display = extra.get("display")
        if display is not None and isinstance(display, nbtlib.Compound):
            custom_name = display.get("Name")
            if custom_name is not None:
                result["customName"] = str(custom_name)

    return result

def dict_to_nbt_item(item_dict, slot=None, is_armor=False, is_offhand=False):
    if not item_dict or not item_dict.get("name") or item_dict["name"] == "minecraft:air":
        comp = {
            "Name": nbtlib.String(""),
            "Count": nbtlib.Byte(0),
            "Damage": nbtlib.Short(0),
            "WasPickedUp": nbtlib.Byte(0),
        }
        if not is_armor and not is_offhand and slot is not None:
            comp["Slot"] = nbtlib.Byte(int(slot))
        return nbtlib.Compound(comp)

    item_name = str(item_dict["name"])
    count = max(1, min(127, int(item_dict.get("count", 1))))
    damage = int(item_dict.get("damage", 0))

    orig_raw = item_dict.get("_raw")
    if orig_raw and isinstance(orig_raw, dict) and str(orig_raw.get("Name", "")) == item_name:
        tag = py_to_nbt(orig_raw)
        tag["Count"] = nbtlib.Byte(count)
        tag["Damage"] = nbtlib.Short(damage)
        tag["WasPickedUp"] = nbtlib.Byte(0)
    else:
        tag = nbtlib.Compound({
            "Name": nbtlib.String(item_name),
            "Count": nbtlib.Byte(count),
            "Damage": nbtlib.Short(damage),
            "WasPickedUp": nbtlib.Byte(0),
        })
        block_keywords = [
            'sponge', 'wet_sponge', 'grass', 'dirt', 'stone', 'cobblestone',
            'chest', 'torch', 'sand', 'gravel', 'planks', 'obsidian', 'bedrock',
            'tnt', 'bookshelf', 'glass', 'furnace', 'table', 'wool', 'ore', 'log', 'wood'
        ]
        short_name = item_name.replace('minecraft:', '').lower()
        if any(kw in short_name for kw in block_keywords) or 'block' in short_name:
            tag["Block"] = nbtlib.Compound({
                "name": nbtlib.String(item_name),
                "states": nbtlib.Compound({}),
                "version": nbtlib.Int(18168865),
            })

    # Slot tag is ONLY for Inventory items in Bedrock; Armor and Offhand NEVER have Slot tag
    if not is_armor and not is_offhand and slot is not None:
        tag["Slot"] = nbtlib.Byte(int(slot))
    elif "Slot" in tag and (is_armor or is_offhand):
        del tag["Slot"]

    enchantments = item_dict.get("enchantments", [])
    custom_name = item_dict.get("customName", "")

    if enchantments or custom_name:
        if "tag" in tag and isinstance(tag["tag"], nbtlib.Compound):
            extra = tag["tag"]
        else:
            extra = nbtlib.Compound({
                "Damage": nbtlib.Int(0),
                "RepairCost": nbtlib.Int(0),
            })

        if enchantments:
            extra["ench"] = nbtlib.List[nbtlib.Compound]([
                nbtlib.Compound({
                    "id": nbtlib.Short(int(e["id"])),
                    "lvl": nbtlib.Short(int(e["lvl"])),
                })
                for e in enchantments
            ])
        elif "ench" in extra:
            del extra["ench"]

        if custom_name:
            if "display" not in extra or not isinstance(extra["display"], nbtlib.Compound):
                extra["display"] = nbtlib.Compound({})
            extra["display"]["Name"] = nbtlib.String(custom_name)
        elif "display" in extra and "Name" in extra["display"]:
            del extra["display"]["Name"]
            if not extra["display"]:
                del extra["display"]

        tag["tag"] = extra
    elif "tag" in tag and not enchantments and not custom_name:
        if "ench" in tag["tag"]: del tag["tag"]["ench"]
        if "display" in tag["tag"]: del tag["tag"]["display"]
        if not tag["tag"] or (len(tag["tag"]) <= 2 and "Damage" in tag["tag"] and "RepairCost" in tag["tag"] and not orig_raw):
            del tag["tag"]

    return tag

def parse_inventory(root):
    result = {
        "hotbar": [None] * 9,
        "main": [None] * 27,
        "armor": [None] * 4,
        "offhand": [None],
    }

    inv_tag = root.get("Inventory")
    if inv_tag is not None:
        for item_tag in inv_tag:
            item = nbt_item_to_dict(item_tag)
            if item:
                slot = item["slot"]
                if 0 <= slot <= 8:
                    result["hotbar"][slot] = item
                elif 9 <= slot <= 35:
                    result["main"][slot - 9] = item

    armor_tag = root.get("Armor")
    if armor_tag is not None:
        for i, item_tag in enumerate(armor_tag):
            if i >= 4:
                break
            item = nbt_item_to_dict(item_tag)
            if item:
                item["slot"] = i
                result["armor"][i] = item

    offhand_tag = root.get("Offhand")
    if offhand_tag is not None:
        for item_tag in offhand_tag:
            item = nbt_item_to_dict(item_tag)
            if item:
                item["slot"] = 0
                result["offhand"][0] = item
            break

    return result

def update_inventory(root, inventory):
    # Hotbar & Main storage items
    items = []
    for slot_idx in range(9):
        item = inventory["hotbar"][slot_idx] if slot_idx < len(inventory["hotbar"]) else None
        if item and item.get("name"):
            items.append(dict_to_nbt_item(item, slot=slot_idx, is_armor=False, is_offhand=False))

    for slot_idx in range(27):
        item = inventory["main"][slot_idx] if slot_idx < len(inventory["main"]) else None
        if item and item.get("name"):
            items.append(dict_to_nbt_item(item, slot=slot_idx + 9, is_armor=False, is_offhand=False))

    root["Inventory"] = nbtlib.List[nbtlib.Compound](items) if items else nbtlib.List[nbtlib.Compound]()

    # Armor slots (Bedrock uses 4 or 5 items; slot 4 is body/wolf armor slot in Bedrock 1.20.80+)
    orig_armor_len = len(root.get("Armor", []))
    armor_count = max(4, orig_armor_len)
    armor_items = []
    for i in range(armor_count):
        if i < 4:
            item = inventory["armor"][i] if (inventory.get("armor") and i < len(inventory["armor"])) else None
            if item and item.get("name"):
                armor_items.append(dict_to_nbt_item(item, is_armor=True, is_offhand=False))
            else:
                armor_items.append(dict_to_nbt_item(None, is_armor=True, is_offhand=False))
        else:
            if orig_armor_len > 4:
                armor_items.append(root["Armor"][4])
            else:
                armor_items.append(dict_to_nbt_item(None, is_armor=True, is_offhand=False))

    root["Armor"] = nbtlib.List[nbtlib.Compound](armor_items)

    # Offhand slot
    offhand_item = inventory["offhand"][0] if (inventory.get("offhand") and len(inventory["offhand"]) > 0 and inventory["offhand"][0]) else None
    if offhand_item and offhand_item.get("name"):
        root["Offhand"] = nbtlib.List[nbtlib.Compound]([dict_to_nbt_item(offhand_item, is_armor=False, is_offhand=True)])
    else:
        root["Offhand"] = nbtlib.List[nbtlib.Compound]([dict_to_nbt_item(None, is_armor=False, is_offhand=True)])

def find_db_folder(extract_dir):
    direct_db = os.path.join(extract_dir, 'db')
    if os.path.isdir(direct_db):
        return direct_db, extract_dir

    for root_dir, dirs, files in os.walk(extract_dir):
        if '__MACOSX' in root_dir:
            continue
        if 'db' in dirs:
            candidate = os.path.join(root_dir, 'db')
            if any(f.endswith(('.ldb', '.log')) or f in ('CURRENT', 'MANIFEST') for f in os.listdir(candidate)):
                return candidate, root_dir

    return None, None

def find_player_key_and_data(db):
    """
    Searches LevelDB for player records:
    1. ~local_player (singleplayer)
    2. player_server_<UUID> or player_<UUID> (Xbox / server / realm downloads)
    """
    try:
        val = db.get(b"~local_player")
        return b"~local_player", val
    except KeyError:
        pass

    try:
        keys = list(db.keys())
    except Exception:
        keys = []

    for k in keys:
        if k.startswith(b"player_server_") or k.startswith(b"player_"):
            return k, db.get(k)

    for k in keys:
        if b"player" in k or b"local" in k:
            return k, db.get(k)

    return None, None

@app.route('/')
def index():
    if ITEMS_DB is None:
        load_data()
    return render_template(
        'index.html',
        items_json=json.dumps(ITEMS_DB, ensure_ascii=False),
        enchantments_json=json.dumps(ENCHANTMENTS_DB, ensure_ascii=False),
    )

@app.route('/upload', methods=['POST'])
def upload():
    if 'world' not in request.files:
        return jsonify({"error": "No world file uploaded."}), 400

    file = request.files['world']
    if not file or not file.filename:
        return jsonify({"error": "No file selected."}), 400

    temp_dir = tempfile.mkdtemp(prefix='mc_inv_')
    filename = file.filename or 'world.mcworld'
    base_name = os.path.splitext(filename)[0]
    upload_path = os.path.join(temp_dir, filename)
    file.save(upload_path)

    is_standalone_dat = filename.lower().endswith('.dat')
    if is_standalone_dat:
        try:
            with open(upload_path, 'rb') as f:
                raw_bytes = f.read()
            nbt_file = load_nbt_data(raw_bytes)
            inventory = parse_inventory(nbt_file)
            session_id = os.path.basename(temp_dir)
            sessions[session_id] = {
                'temp_dir': temp_dir,
                'is_dat': True,
                'dat_path': upload_path,
                'original_filename': filename,
                'base_name': base_name,
            }
            return jsonify({
                "sessionId": session_id,
                "inventory": inventory,
                "filename": filename,
            })
        except Exception as e:
            shutil.rmtree(temp_dir, ignore_errors=True)
            return jsonify({"error": f"Failed to parse .dat file: {str(e)}"}), 400

    extract_dir = os.path.join(temp_dir, 'extracted')
    try:
        with zipfile.ZipFile(upload_path, 'r') as zf:
            zf.extractall(extract_dir)
    except zipfile.BadZipFile:
        shutil.rmtree(temp_dir, ignore_errors=True)
        return jsonify({"error": "Invalid world archive. Please upload a valid .mcworld or .zip world file."}), 400

    db_path, world_root = find_db_folder(extract_dir)
    if not db_path:
        shutil.rmtree(temp_dir, ignore_errors=True)
        return jsonify({"error": "Could not find a valid LevelDB 'db' folder in the uploaded world archive."}), 400

    try:
        from leveldb import LevelDB
        db = LevelDB(db_path)
        player_key, player_data = find_player_key_and_data(db)
        if not player_key or not player_data:
            db.close()
            shutil.rmtree(temp_dir, ignore_errors=True)
            return jsonify({"error": "Could not find player inventory data inside LevelDB."}), 400

        nbt_file = load_nbt_data(player_data)
        inventory = parse_inventory(nbt_file)
        db.close()

    except ImportError:
        shutil.rmtree(temp_dir, ignore_errors=True)
        return jsonify({"error": "LevelDB library not installed on the server."}), 500
    except Exception as e:
        shutil.rmtree(temp_dir, ignore_errors=True)
        return jsonify({"error": f"Error reading Bedrock world data: {str(e)}"}), 500

    session_id = os.path.basename(temp_dir)
    sessions[session_id] = {
        'temp_dir': temp_dir,
        'extract_dir': extract_dir,
        'world_root': world_root,
        'db_path': db_path,
        'player_key': player_key,
        'original_filename': filename,
        'base_name': base_name,
        'is_dat': False,
    }

    return jsonify({
        "sessionId": session_id,
        "inventory": inventory,
        "filename": filename,
    })

@app.route('/save', methods=['POST'])
def save():
    data = request.json or {}
    session_id = data.get('sessionId')
    inventory = data.get('inventory')
    export_format = (data.get('format') or 'mcworld').lower().strip('.')

    if not session_id or not inventory:
        return jsonify({"error": "Missing sessionId or inventory data."}), 400

    session = sessions.get(session_id)
    if not session:
        return jsonify({"error": "Session expired. Please re-upload your world."}), 400

    # Standalone .dat file
    if session.get('is_dat'):
        try:
            with open(session['dat_path'], 'rb') as f:
                raw_bytes = f.read()
            nbt_file = load_nbt_data(raw_bytes)
            update_inventory(nbt_file, inventory)
            new_data = save_bedrock_nbt(nbt_file)
            output_filename = f"{session['base_name']}_modified.dat"
            output_path = os.path.join(session['temp_dir'], output_filename)
            with open(output_path, 'wb') as f:
                f.write(new_data)

            return send_file(
                output_path,
                as_attachment=True,
                download_name=output_filename,
                mimetype='application/octet-stream',
            )
        except Exception as e:
            return jsonify({"error": f"Error saving .dat file: {str(e)}"}), 500

    # Bedrock World Archive (LevelDB)
    try:
        from leveldb import LevelDB
        db = LevelDB(session['db_path'])
        player_key = session.get('player_key', b'~local_player')
        player_data = db.get(player_key)
        nbt_file = load_nbt_data(player_data)
        update_inventory(nbt_file, inventory)
        new_data = save_bedrock_nbt(nbt_file)
        db.put(player_key, new_data)
        # Also ensure ~local_player is updated for seamless singleplayer iOS loading
        if player_key != b'~local_player':
            try:
                db.put(b'~local_player', new_data)
            except Exception:
                pass
        db.close()
    except Exception as e:
        return jsonify({"error": f"Error updating world inventory: {str(e)}"}), 500

    base_name = session.get('base_name', 'world')
    ext = 'mcworld' if export_format == 'mcworld' else 'zip'
    output_filename = f"{base_name}_modified.{ext}"
    output_path = os.path.join(session['temp_dir'], output_filename)
    world_root = session.get('world_root', session['extract_dir'])

    with zipfile.ZipFile(output_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for dirpath, dirnames, filenames in os.walk(world_root):
            if '__MACOSX' in dirpath:
                continue
            for fname in filenames:
                if fname == '.DS_Store' or fname.startswith('._'):
                    continue
                file_path = os.path.join(dirpath, fname)
                arc_name = os.path.relpath(file_path, world_root)
                zf.write(file_path, arc_name)

    mimetype = 'application/x-minecraft-world' if ext == 'mcworld' else 'application/zip'
    return send_file(
        output_path,
        as_attachment=True,
        download_name=output_filename,
        mimetype=mimetype,
    )

if __name__ == '__main__':
    load_data()
    app.run(debug=True, host='0.0.0.0', port=5000)
