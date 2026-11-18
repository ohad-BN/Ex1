python-only-web-ui
Enforces a strict Python-only limitation for web development and UI tasks. Use whenever the user asks for web apps, assignments, or front-end work.

Instructions
Python-Only Web UI Constraint
Summary
You must strictly stick to Python and its libraries for any web development, assignment, or UI task. Never suggest or output React, Java, TypeScript, JavaScript, or any other non-Python web technologies.

When to Use
When the user asks for help building a web-based assignment or UI.
When generating front-end code for a web project.
Rules & Steps
Strictly Python: Use pure Python libraries designed for web and UI development (e.g., Streamlit, Gradio, Reflex, NiceGUI, Flet, or Dash).
No External Frontend Code: Do not write or suggest using JavaScript, TypeScript, HTML/CSS (unless strictly required within a Python wrapper), React, Angular, Vue, or Java.
Library Selection: Default to Streamlit or Gradio for quick assignments. Use Reflex, NiceGUI, or Flet if a more customized, traditional component-based UI is required.
Explanation: Always explain how the chosen Python library handles the frontend rendering automatically under the hood so the user understands why no JS/TS is needed.
Gotchas
Do not accidentally drop into Flask/Django + HTML/JS templates. Stick strictly to Python-native UI libraries where the UI is written entirely in Python.