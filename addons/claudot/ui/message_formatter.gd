extends RefCounted

## MessageFormatter - Static utility for BBCode message formatting
##
## Provides methods to format user, assistant, and system messages with
## color-coded labels and code block highlighting for RichTextLabel display.


## Format user message with green label and role indicator
static func format_user_message(text: String) -> String:
	var bbcode = "[b][color=#8ec07c]> You:[/color][/b] %s\n\n" % text
	return bbcode


## Format assistant message with blue label, role indicator, and code block processing
static func format_assistant_message(text: String) -> String:
	var bbcode = "[b][color=#83a598]< Claude:[/color][/b] "

	# Check if message contains code blocks
	if "```" in text:
		bbcode += _format_code_blocks(text)
	else:
		bbcode += "%s\n\n" % text

	return bbcode


## Format system message with italic yellow text and role indicator
static func format_system_message(text: String) -> String:
	var bbcode = "[i][color=#fabd2f]- System: %s[/color][/i]\n\n" % text
	return bbcode


## Private method to process code blocks in message text
## Splits on triple backticks, formats code with monospace + dark background
static func _format_code_blocks(text: String) -> String:
	var parts = text.split("```")
	var formatted = ""

	for i in parts.size():
		if i % 2 == 0:
			# Regular text (even indices)
			formatted += parts[i]
		else:
			# Code block (odd indices)
			var code_content = parts[i]

			# Strip language identifier (first line if present)
			var lines = code_content.split("\n", false)
			if lines.size() > 0:
				# Check if first line looks like a language identifier (no spaces, short)
				if lines.size() > 1 and lines[0].strip_edges().length() < 20 and not " " in lines[0].strip_edges():
					# Skip first line (language identifier)
					code_content = "\n".join(lines.slice(1))
				# else: Keep all lines (no language identifier or multiline first line)

			# Wrap in BBCode for code styling
			formatted += "[code][bgcolor=#282828][color=#ebdbb2]%s[/color][/bgcolor][/code]" % code_content

	formatted += "\n\n"
	return formatted
