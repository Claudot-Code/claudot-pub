@tool
extends RefCounted

## PropertyConverter - Converts Godot types to/from JSON-safe representations
##
## Godot types like Vector2, Color, Transform3D etc. are not JSON-serializable.
## This converter provides bidirectional conversion.

## Convert Godot Variant to JSON-safe representation
## @param value: Any Godot Variant
## @return: JSON-serializable value (primitives, dicts, arrays)
static func variant_to_json(value: Variant) -> Variant:
	# Handle null
	if value == null:
		return null

	var value_type = typeof(value)

	# Handle basic JSON types - pass through
	match value_type:
		TYPE_NIL:
			return null
		TYPE_BOOL, TYPE_INT, TYPE_FLOAT, TYPE_STRING:
			return value

	# Handle Godot types - convert to structured dicts
	match value_type:
		TYPE_VECTOR2:
			return {
				"_type": "Vector2",
				"x": value.x,
				"y": value.y
			}

		TYPE_VECTOR2I:
			return {
				"_type": "Vector2i",
				"x": value.x,
				"y": value.y
			}

		TYPE_VECTOR3:
			return {
				"_type": "Vector3",
				"x": value.x,
				"y": value.y,
				"z": value.z
			}

		TYPE_VECTOR3I:
			return {
				"_type": "Vector3i",
				"x": value.x,
				"y": value.y,
				"z": value.z
			}

		TYPE_VECTOR4:
			return {
				"_type": "Vector4",
				"x": value.x,
				"y": value.y,
				"z": value.z,
				"w": value.w
			}

		TYPE_VECTOR4I:
			return {
				"_type": "Vector4i",
				"x": value.x,
				"y": value.y,
				"z": value.z,
				"w": value.w
			}

		TYPE_COLOR:
			return {
				"_type": "Color",
				"r": value.r,
				"g": value.g,
				"b": value.b,
				"a": value.a
			}

		TYPE_RECT2:
			return {
				"_type": "Rect2",
				"position": variant_to_json(value.position),
				"size": variant_to_json(value.size)
			}

		TYPE_RECT2I:
			return {
				"_type": "Rect2i",
				"position": variant_to_json(value.position),
				"size": variant_to_json(value.size)
			}

		TYPE_TRANSFORM2D:
			return {
				"_type": "Transform2D",
				"origin": variant_to_json(value.origin),
				"x": variant_to_json(value.x),
				"y": variant_to_json(value.y)
			}

		TYPE_TRANSFORM3D:
			return {
				"_type": "Transform3D",
				"origin": variant_to_json(value.origin),
				"basis": {
					"x": variant_to_json(value.basis.x),
					"y": variant_to_json(value.basis.y),
					"z": variant_to_json(value.basis.z)
				}
			}

		TYPE_QUATERNION:
			return {
				"_type": "Quaternion",
				"x": value.x,
				"y": value.y,
				"z": value.z,
				"w": value.w
			}

		TYPE_NODE_PATH:
			return {
				"_type": "NodePath",
				"path": str(value)
			}

		TYPE_ARRAY:
			var result = []
			for item in value:
				result.append(variant_to_json(item))
			return result

		TYPE_DICTIONARY:
			var result = {}
			for key in value:
				# Keys must be strings for JSON
				var key_str = str(key)
				result[key_str] = variant_to_json(value[key])
			return result

		_:
			# Fallback: use var_to_str() for unknown types
			return {
				"_type": "raw",
				"value": var_to_str(value)
			}


## Convert JSON-safe value back to Godot Variant
## @param value: JSON value (possibly with _type metadata)
## @param target_type: Optional Variant.Type hint for conversion
## @return: Godot Variant
static func json_to_variant(value: Variant, target_type: int = -1) -> Variant:
	# Handle null
	if value == null:
		return null

	# If it's a dictionary with _type, reconstruct Godot type
	if typeof(value) == TYPE_DICTIONARY and value.has("_type"):
		var type_name = value["_type"]

		match type_name:
			"Vector2":
				return Vector2(value.get("x", 0.0), value.get("y", 0.0))

			"Vector2i":
				return Vector2i(value.get("x", 0), value.get("y", 0))

			"Vector3":
				return Vector3(value.get("x", 0.0), value.get("y", 0.0), value.get("z", 0.0))

			"Vector3i":
				return Vector3i(value.get("x", 0), value.get("y", 0), value.get("z", 0))

			"Vector4":
				return Vector4(value.get("x", 0.0), value.get("y", 0.0), value.get("z", 0.0), value.get("w", 0.0))

			"Vector4i":
				return Vector4i(value.get("x", 0), value.get("y", 0), value.get("z", 0), value.get("w", 0))

			"Color":
				return Color(value.get("r", 0.0), value.get("g", 0.0), value.get("b", 0.0), value.get("a", 1.0))

			"Rect2":
				var pos = json_to_variant(value.get("position", {}))
				var size = json_to_variant(value.get("size", {}))
				return Rect2(pos, size)

			"Rect2i":
				var pos = json_to_variant(value.get("position", {}))
				var size = json_to_variant(value.get("size", {}))
				return Rect2i(pos, size)

			"Transform2D":
				var origin = json_to_variant(value.get("origin", {}))
				var x = json_to_variant(value.get("x", {}))
				var y = json_to_variant(value.get("y", {}))
				return Transform2D(x, y, origin)

			"Transform3D":
				var origin = json_to_variant(value.get("origin", {}))
				var basis_data = value.get("basis", {})
				var basis_x = json_to_variant(basis_data.get("x", {}))
				var basis_y = json_to_variant(basis_data.get("y", {}))
				var basis_z = json_to_variant(basis_data.get("z", {}))
				var basis = Basis(basis_x, basis_y, basis_z)
				return Transform3D(basis, origin)

			"Quaternion":
				return Quaternion(value.get("x", 0.0), value.get("y", 0.0), value.get("z", 0.0), value.get("w", 1.0))

			"NodePath":
				return NodePath(value.get("path", ""))

			"raw":
				# Use str_to_var() for raw Godot serialized values
				return str_to_var(value.get("value", ""))

	# Handle arrays recursively
	if typeof(value) == TYPE_ARRAY:
		var result = []
		for item in value:
			result.append(json_to_variant(item))
		return result

	# Handle dictionaries recursively (but not those with _type, already handled)
	if typeof(value) == TYPE_DICTIONARY:
		var result = {}
		for key in value:
			result[key] = json_to_variant(value[key])
		return result

	# For basic types, return as-is
	return value
