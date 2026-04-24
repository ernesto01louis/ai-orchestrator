Fix this {{language}} project that crashed.

TASK: {{prompt}}

ENTRYPOINT: {{entrypoint}}

ERROR:
{{error}}

FILES:
{{formatted_files}}

RULES:
- Return ALL files with the fix applied
- Mark each file with: # === FILE: filename ===
- Return ONLY code, no explanations
