extends RefCounted
class_name JsonHighlighter

## JsonHighlighter - Provides syntax highlighting configuration for JSON text
##
## Static utility that creates and configures a CodeHighlighter for JSON display.

static func create() -> CodeHighlighter:
	## Create a configured CodeHighlighter for JSON syntax highlighting.
	var highlighter = CodeHighlighter.new()
	highlighter.number_color = Color("#d3869b")         # Purple for numbers
	highlighter.symbol_color = Color("#fabd2f")          # Yellow for brackets, colons, commas
	highlighter.function_color = Color("#fabd2f")        # Yellow (unused but set)
	highlighter.member_variable_color = Color("#83a598") # Blue (unused but set)

	# String regions (between double quotes)
	highlighter.add_color_region('"', '"', Color("#b8bb26"), false)  # Green for strings

	# Keywords
	highlighter.add_keyword_color("true", Color("#83a598"))   # Blue
	highlighter.add_keyword_color("false", Color("#83a598"))  # Blue
	highlighter.add_keyword_color("null", Color("#fe8019"))   # Orange

	return highlighter
