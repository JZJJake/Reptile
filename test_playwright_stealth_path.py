import playwright_stealth
import os

base_dir = os.path.dirname(playwright_stealth.__file__)
js_dir = os.path.join(base_dir, 'js')
print("Base:", base_dir)
print("JS:", js_dir)
