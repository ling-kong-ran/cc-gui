"""
Config Manager - 管理 CCB 配置文件（settings.json、skills、agents）
"""
import json
import os
from pathlib import Path
from typing import Any

CLAUDE_DIR = Path(os.environ.get("USERPROFILE", "~")) / ".claude"
SETTINGS_FILE = CLAUDE_DIR / "settings.json"
SKILLS_DIR = CLAUDE_DIR / "skills"
AGENTS_DIR = CLAUDE_DIR / "agents"

# 项目级配置
PROJECT_DIR = Path(__file__).parent.parent
PROJECT_SETTINGS = PROJECT_DIR / "settings.json"


def get_settings() -> dict[str, Any]:
    """读取全局 settings.json"""
    if SETTINGS_FILE.exists():
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    return {}


def save_settings(data: dict[str, Any]):
    """保存全局 settings.json"""
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def get_env_config() -> dict[str, str]:
    """获取 env 配置段"""
    settings = get_settings()
    return settings.get("env", {})


def update_env_config(env: dict[str, str]):
    """更新 env 配置段"""
    settings = get_settings()
    settings["env"] = env
    save_settings(settings)


def list_skills() -> list[dict[str, str]]:
    """列出所有已安装的 skills"""
    skills = []
    if not SKILLS_DIR.exists():
        return skills

    for skill_dir in SKILLS_DIR.iterdir():
        if skill_dir.is_dir():
            skill_file = skill_dir / "SKILL.md"
            if skill_file.exists():
                content = skill_file.read_text(encoding="utf-8")
                # 解析 frontmatter
                name = skill_dir.name
                description = ""
                if content.startswith("---"):
                    parts = content.split("---", 2)
                    if len(parts) >= 3:
                        for line in parts[1].strip().split("\n"):
                            if line.startswith("name:"):
                                name = line.split(":", 1)[1].strip().strip('"\'')
                            elif line.startswith("description:"):
                                description = line.split(":", 1)[1].strip().strip('"\'')
                skills.append({
                    "name": name,
                    "dir": skill_dir.name,
                    "description": description,
                })
    return skills


def list_agents() -> list[dict[str, str]]:
    """列出所有已配置的 agents"""
    agents = []
    if not AGENTS_DIR.exists():
        return agents

    for agent_file in AGENTS_DIR.iterdir():
        if agent_file.suffix == ".md":
            content = agent_file.read_text(encoding="utf-8")
            name = agent_file.stem
            description = ""

            # 尝试解析 frontmatter
            if content.startswith("---"):
                parts = content.split("---", 2)
                if len(parts) >= 3:
                    for line in parts[1].strip().split("\n"):
                        if line.startswith("name:"):
                            name = line.split(":", 1)[1].strip().strip('"\'')
                        elif line.startswith("description:"):
                            description = line.split(":", 1)[1].strip().strip('"\'')

            agents.append({
                "name": name,
                "file": agent_file.name,
                "description": description,
            })
    return agents


def get_available_models() -> list[str]:
    """获取可用模型列表"""
    env = get_env_config()
    models = []

    # 从环境变量中提取已配置的模型
    for key, value in env.items():
        if "MODEL" in key and value:
            models.append(value)

    # 确保有默认模型
    if not models:
        models = ["claude-opus-4-6", "claude-sonnet-4-6", "claude-haiku-4-6"]

    return list(set(models))
