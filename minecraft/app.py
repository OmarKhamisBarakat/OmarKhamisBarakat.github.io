import os
import uuid
import json
import io
import shutil
import zipfile
import threading
from werkzeug.utils import secure_filename
from flask import Flask, request, jsonify, render_template, send_file, session
from flask_cors import CORS
import nbtlib

try:
    import plyvel
except ImportError:
    pass

try:
    from leveldb import LevelDB
except ImportError:
    pass

app = Flask(__name__, template_folder='templates', static_folder='static')
app.secret_key = 'super_secret_key'
CORS(app)

UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), 'uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

def py_to_nbt(val, hint_key=None):
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

def dict_to_nbt_item(item_dict, slot=None, is_armor=False, is_offhand=False, orig_item=None):
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

    if orig_item and str(orig_item.get("Name", "")) == item_name:
        import copy
        tag = copy.deepcopy(orig_item)
        tag["Count"] = nbtlib.Byte(count)
        tag["Damage"] = nbtlib.Short(damage)
        if not is_armor and not is_offhand and slot is not None:
            tag["Slot"] = nbtlib.Byte(int(slot))
        return tag

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
            'tnt', 'bookshelf', 'glass', 'furnace', 'table', 'wool', 'ore', 'log', 'wood', 'repeater', 'redstone'
        ]
        short_name = item_name.replace('minecraft:', '').lower()
        if any(k in short_name for k in block_keywords):
            tag["Block"] = nbtlib.Compound({
                "name": nbtlib.String(item_name),
                "states": nbtlib.Compound({}),
                "version": nbtlib.Int(17959425)
            })

    enchs = item_dict.get("enchantments", [])
    if enchs:
        tag["tag"] = nbtlib.Compound({
            "ench": nbtlib.List[nbtlib.Compound]([
                nbtlib.Compound({
                    "id": nbtlib.Short(e.get("id", 0)),
                    "lvl": nbtlib.Short(e.get("lvl", 1))
                }) for e in enchs
            ])
        })

    if not is_armor and not is_offhand and slot is not None:
        tag["Slot"] = nbtlib.Byte(int(slot))

    return tag

def parse_inventory(root):
    inventory = {
        "hotbar": {},
        "main": {},
        "armor": {},
        "offhand": {},
        "stats": {}
    }

    inv_tag = root.get("Inventory")
    if inv_tag is not None:
        for item_tag in inv_tag:
            item = nbt_item_to_dict(item_tag)
            if item:
                slot = item["slot"]
                if 0 <= slot <= 8:
                    inventory["hotbar"][str(slot)] = item
                elif 9 <= slot <= 35:
                    inventory["main"][str(slot - 9)] = item

    armor_tag = root.get("Armor")
    if armor_tag is not None:
        keys = ['helmet', 'chestplate', 'leggings', 'boots']
        for idx, item_tag in enumerate(armor_tag):
            if idx < 4:
                item = nbt_item_to_dict(item_tag)
                if item:
                    inventory["armor"][keys[idx]] = item

    offhand_tag = root.get("Offhand")
    if offhand_tag is not None and len(offhand_tag) > 0:
        item = nbt_item_to_dict(offhand_tag[0])
        if item:
            inventory["offhand"]["0"] = item

    attr_tag = root.get("Attributes")
    if attr_tag is not None:
        for attr in attr_tag:
            name = str(attr.get("Name", ""))
            val = float(attr.get("Current", 0))
            if name == "minecraft:health":
                inventory["stats"]["health"] = val
            elif name == "minecraft:player.hunger":
                inventory["stats"]["hunger"] = val
            elif name == "minecraft:player.level":
                inventory["stats"]["xp_level"] = val

    return inventory

def update_inventory(root, inventory):
    orig_items = {}
    for inv_list in ["Inventory", "Armor", "Offhand"]:
        tag_list = root.get(inv_list)
        if tag_list:
            for i, item in enumerate(tag_list):
                if inv_list == "Inventory" and "Slot" in item:
                    orig_items[int(item["Slot"])] = item
                elif inv_list == "Armor":
                    orig_items[100 + i] = item
                elif inv_list == "Offhand":
                    orig_items[200 + i] = item

    items = []
    for slot_idx in range(9):
        item = inventory.get("hotbar", {}).get(str(slot_idx))
        if item:
            nbt_item = dict_to_nbt_item(item, slot_idx, orig_item=orig_items.get(slot_idx))
            if nbt_item: items.append(nbt_item)

    for slot_idx in range(27):
        item = inventory.get("main", {}).get(str(slot_idx))
        if item:
            nbt_item = dict_to_nbt_item(item, slot_idx + 9, orig_item=orig_items.get(slot_idx + 9))
            if nbt_item: items.append(nbt_item)

    if items:
        root["Inventory"] = nbtlib.List[nbtlib.Compound](items)
    else:
        root["Inventory"] = nbtlib.List[nbtlib.Compound]()

    armor_items = []
    for slot_idx, key in enumerate(['helmet', 'chestplate', 'leggings', 'boots']):
        item = inventory.get("armor", {}).get(key)
        if item:
            nbt_item = dict_to_nbt_item(item, is_armor=True, orig_item=orig_items.get(100 + slot_idx))
            if nbt_item: armor_items.append(nbt_item)
    if armor_items:
        root["Armor"] = nbtlib.List[nbtlib.Compound](armor_items)
    else:
        root["Armor"] = nbtlib.List[nbtlib.Compound]()

    offhand_items = []
    item = inventory.get("offhand", {}).get("0")
    if item:
        nbt_item = dict_to_nbt_item(item, is_offhand=True, orig_item=orig_items.get(200))
        if nbt_item: offhand_items.append(nbt_item)
    if offhand_items:
        root["Offhand"] = nbtlib.List[nbtlib.Compound](offhand_items)
    else:
        root["Offhand"] = nbtlib.List[nbtlib.Compound]()

    if "stats" in inventory:
        attr_tag = root.get("Attributes")
        if attr_tag is not None:
            stats = inventory["stats"]
            for attr in attr_tag:
                name = str(attr.get("Name", ""))
                if name == "minecraft:health" and "health" in stats:
                    attr["Current"] = nbtlib.Float(stats["health"])
                elif name == "minecraft:player.hunger" and "hunger" in stats:
                    attr["Current"] = nbtlib.Float(stats["hunger"])
                elif name == "minecraft:player.level" and "xp_level" in stats:
                    attr["Current"] = nbtlib.Float(stats["xp_level"])


def find_db_folder(extract_dir):
    direct_db = os.path.join(extract_dir, 'db')
    if os.path.isdir(direct_db):
        return direct_db, extract_dir

    for root_dir, dirs, files in os.walk(extract_dir):
        if '__MACOSX' in root_dir:
            continue
        if 'db' in dirs:
            return os.path.join(root_dir, 'db'), root_dir

    return None, None

def find_player_key_and_data(db):
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

    return None, None

def load_nbt_data(raw_bytes):
    try:
        return nbtlib.File.parse(io.BytesIO(raw_bytes), byteorder='little')
    except Exception:
        pass

    if len(raw_bytes) > 8 and raw_bytes[8] == 0x0A:
        try:
            return nbtlib.File.parse(io.BytesIO(raw_bytes[8:]), byteorder='little')
        except Exception:
            pass

    if raw_bytes.startswith(b'\x0a'):
        try:
            return nbtlib.File.parse(io.BytesIO(raw_bytes), byteorder='little')
        except Exception:
            pass

    try:
        compound = nbtlib.Compound.parse(io.BytesIO(raw_bytes), byteorder='little')
        return nbtlib.File(compound)
    except Exception:
        pass

    return None

def save_bedrock_nbt(nbt_file):
    buf = io.BytesIO()
    nbt_file.write(buf, byteorder="little")
    return buf.getvalue()

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/upload', methods=['POST'])
def upload():
    if 'file' not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({"error": "No selected file"}), 400

    session_id = f"mc_inv_{uuid.uuid4().hex[:8]}"
    temp_dir = os.path.join(app.config['UPLOAD_FOLDER'], session_id)
    os.makedirs(temp_dir, exist_ok=True)
    
    filename = secure_filename(file.filename)
    filepath = os.path.join(temp_dir, filename)
    file.save(filepath)

    is_standalone_dat = filename.lower().endswith('.dat')

    extract_dir = os.path.join(temp_dir, "extracted")
    os.makedirs(extract_dir, exist_ok=True)

    db_path = None
    world_root = extract_dir
    nbt_file = None

    if is_standalone_dat:
        try:
            nbt_file = nbtlib.load(filepath, gzipped=False, byteorder="little")
            session['is_dat'] = True
            session['dat_path'] = filepath
        except Exception as e:
            return jsonify({"error": f"Failed to parse .dat file: {e}"}), 400
    else:
        try:
            with zipfile.ZipFile(filepath, 'r') as z:
                z.extractall(extract_dir)
        except Exception as e:
            return jsonify({"error": f"Failed to extract zip: {e}"}), 400
        
        db_path, world_root = find_db_folder(extract_dir)
        if not db_path:
            return jsonify({"error": "Could not find LevelDB folder ('db') in the uploaded world."}), 400

        try:
            db = LevelDB(db_path)
        except Exception as e:
            return jsonify({"error": f"Failed to open LevelDB: {e}"}), 500

        player_key, player_data = find_player_key_and_data(db)
        if not player_key:
            return jsonify({"error": "Could not find local player data in LevelDB"}), 400

        nbt_file = load_nbt_data(player_data)
        if not nbt_file:
            return jsonify({"error": "Failed to parse player NBT data"}), 500
        
        db.close()
        
        session['db_path'] = db_path
        session['player_key'] = player_key
        session['world_root'] = world_root

    inventory_data = parse_inventory(nbt_file)

    session['session_id'] = session_id
    session['temp_dir'] = temp_dir
    session['extract_dir'] = extract_dir
    session['base_name'] = os.path.splitext(filename)[0]

    return jsonify({
        "sessionId": session_id,
        "inventory": inventory_data
    })

@app.route('/clear_chunk', methods=['POST'])
def clear_chunk():
    data = request.json
    if not data or 'sessionId' not in data:
        return jsonify({"error": "Invalid request"}), 400
    
    db_path = session.get('db_path')
    if not db_path:
        return jsonify({"error": "No database found for this session."}), 400

    try:
        db = LevelDB(db_path)
    except Exception as e:
        return jsonify({"error": f"Failed to open LevelDB: {e}"}), 500

    try:
        keys = list(db.keys())
    except Exception:
        keys = []

    cleared = 0
    for key in keys:
        if len(key) >= 9 and key[8] == 0x2f:
            db.delete(key)
            cleared += 1

    db.close()
    return jsonify({"success": True, "cleared_subchunks": cleared})


@app.route('/save', methods=['POST'])
def save():
    data = request.json
    if not data or 'sessionId' not in data or 'inventory' not in data:
        return jsonify({"error": "Invalid request"}), 400

    session_id = data['sessionId']
    inventory = data['inventory']
    export_format = data.get('format', 'zip')

    if session.get('is_dat'):
        dat_path = session['dat_path']
        nbt_file = nbtlib.load(dat_path, gzipped=False, byteorder="little")
        update_inventory(nbt_file, inventory)
        output_filename = f"{session['base_name']}_modified.dat"
        output_path = os.path.join(session['temp_dir'], output_filename)
        nbt_file.save(output_path, byteorder="little")
        return send_file(output_path, as_attachment=True, download_name=output_filename, mimetype='application/octet-stream')

    if 'db_path' not in session:
        return jsonify({"error": "Session expired or invalid"}), 400

    try:
        db = LevelDB(session['db_path'])
        
        keys = list(db.keys())
        player_keys = [k for k in keys if k.startswith(b'player_') or k.startswith(b'~local_player')]

        if not player_keys:
            player_keys = [session.get('player_key', b'~local_player')]

        for pk in player_keys:
            player_data = db.get(pk)
            if not player_data: continue
            
            nbt_file = load_nbt_data(player_data)
            if nbt_file:
                update_inventory(nbt_file, inventory)
                new_data = save_bedrock_nbt(nbt_file)
                db.put(pk, new_data)

        db.close()
    except Exception as e:
        return jsonify({"error": f"Failed to save to LevelDB: {e}"}), 500

    base_name = session.get('base_name', 'world')
    ext = 'mcworld' if export_format == 'mcworld' else 'zip'
    output_filename = f"{base_name}_modified.{ext}"
    output_path = os.path.join(session['temp_dir'], output_filename)
    
    pack_root = session.get('world_root', session['extract_dir']) if ext == 'mcworld' else session['extract_dir']

    with zipfile.ZipFile(output_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for dirpath, dirnames, filenames in os.walk(pack_root):
            if '__MACOSX' in dirpath:
                continue
            for fname in filenames:
                if fname == '.DS_Store' or fname.startswith('._'):
                    continue
                file_path = os.path.join(dirpath, fname)
                arc_name = os.path.relpath(file_path, pack_root)
                zf.write(file_path, arc_name)

    mimetype = 'application/x-minecraft-world' if ext == 'mcworld' else 'application/zip'
    return send_file(output_path, as_attachment=True, download_name=output_filename, mimetype=mimetype)


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
