write_file_tool = {
    "name": "write_file",
    "description": "Writes content to a file at the specified path. Overwrites if exists.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "path": {
                "type": "STRING",
                "description": "The path of the file to write to."
            },
            "content": {
                "type": "STRING",
                "description": "The content to write to the file."
            }
        },
        "required": ["path", "content"]
    }
}

read_directory_tool = {
    "name": "read_directory",
    "description": "Lists the contents of a directory.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "path": {
                "type": "STRING",
                "description": "The path of the directory to list."
            }
        },
        "required": ["path"]
    }
}

read_file_tool = {
    "name": "read_file",
    "description": "Reads the content of a file.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "path": {
                "type": "STRING",
                "description": "The path of the file to read."
            }
        },
        "required": ["path"]
    }
}

open_document_tool = {
    "name": "open_document",
    "description": (
        "Opens a file or folder on the user's computer with the default application registered in the "
        "operating system (PDF reader, LibreOffice/Word, image viewer, file manager, etc.). "
        "Use when the user asks to open, show or view a document or file 'with the default program', "
        "'in Word', 'in the PDF viewer', or similar. Prefer this over read_file when they want to see "
        "the file in an external app. Relative paths are resolved inside the current project folder; "
        "absolute paths are allowed if they exist. If the user has enabled an extension allowlist in "
        "Settings, only those file types (and optionally folders) may be opened."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "path": {
                "type": "STRING",
                "description": "Path to the file or folder to open (absolute, or relative to the current project).",
            }
        },
        "required": ["path"],
    },
}

tools_list = [{"function_declarations": [
    write_file_tool,
    read_directory_tool,
    read_file_tool,
    open_document_tool,
]}]


