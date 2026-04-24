Create an implementation plan for this task.

REMEMBER: Prefer built-in modules. Test before deploying. Learn from past failures. Be honest about code quality.

TARGET SYSTEM:
{{env}}

USER TASK:
{{prompt}}

{{memory_context}}

RULES:
- language: choose "python", "bash", or "javascript"
- entrypoint: the main file to execute (e.g. main.py, main.sh, index.js)
- project_type: "script" for run-and-exit programs, "server" for long-running servers/listeners
- execution_mode: "tools_only" if task needs no code generation, "tools_then_generate" if tools prep is needed first, "generate" for all standard code generation tasks
- port: the port number if project_type is "server", otherwise 0
- files: map each filename to a description of its purpose
- dependencies: packages needed (pip for python, npm for javascript, empty for bash)
- For simple tasks, use a single file
- For complex tasks, split into logical modules
- If past failures are listed above, avoid the approaches that failed
