"""dashboard — unified Streamlit multipage UI for the folder-reorg stack.

Made an explicit package (with this __init__.py) so the import
`from dashboard._common import …` resolves unambiguously even when
Streamlit pushes the script's parent directory onto sys.path.
"""
