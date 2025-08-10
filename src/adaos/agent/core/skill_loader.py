import importlib.util
import os
import yaml

class SkillLoader:
    def __init__(self, skills_dir="skills"):
        self.skills_dir = skills_dir
        self.skills = {}

    def load_skills(self):
        for skill_name in os.listdir(self.skills_dir):
            skill_path = os.path.join(self.skills_dir, skill_name)
            manifest_path = os.path.join(skill_path, "manifest.yaml")
            handler_path = os.path.join(skill_path, "handler.py")

            if not os.path.exists(manifest_path) or not os.path.exists(handler_path):
                continue

            with open(manifest_path, "r") as f:
                manifest = yaml.safe_load(f)

            # Загружаем handler.py как модуль
            spec = importlib.util.spec_from_file_location(f"{skill_name}_handler", handler_path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            self.skills[skill_name] = {
                "manifest": manifest,
                "handler": module,
                "permissions": set(manifest.get("permissions", []))
            }

    def get_skill_for_intent(self, intent):
        for skill_name, data in self.skills.items():
            intents = [i for i in data["manifest"].get("intents", [])]
            if intent in intents:
                return data["handler"]
        return None
