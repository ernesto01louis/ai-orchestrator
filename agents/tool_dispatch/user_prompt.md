Decide which tools to run before generating code for this task.

TASK: {{prompt}}

PLAN:
language: {{language}}
files: {{file_list}}
dependencies: {{dependencies}}

TARGET SYSTEM:
OS: {{os}}
Arch: {{arch}}

AVAILABLE TOOLS:
{{tool_descriptions}}

Return a JSON object with a "tools" array. Each entry needs "name" and optionally "args".
If no tools are needed, return: {"tools": []}
