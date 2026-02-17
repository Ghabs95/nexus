"""
AI Orchestrator: Unified interface for Copilot CLI and Gemini CLI (CLI-only mode).

Handles:
- Tool selection based on agent type and workflow tier
- Fallback logic (primary tool → fallback if rate-limited, fails, or unavailable)
- Rate limit detection and state management
- Unified result parsing and validation
"""

import json
import logging
import os
import re
import subprocess
import tempfile
import time
from enum import Enum
from typing import Dict, Optional, Tuple, Any
from pathlib import Path

logger = logging.getLogger(__name__)


class AIProvider(Enum):
    """AI provider enumeration."""
    COPILOT = "copilot"
    GEMINI = "gemini"


class ToolUnavailableError(Exception):
    """Raised when a tool is unavailable or fails."""
    pass


class RateLimitedError(Exception):
    """Raised when a tool hits rate limits."""
    pass


class AIOrchestrator:
    """
    Manages AI tool orchestration with fallback support.
    
    Architecture:
    - Primary tool selected based on agent/workflow config
    - Fallback tool automatically tried if primary fails
    - Rate limit tracking with TTL-based fallback triggers
    - Unified interface for all AI operations
    """

    # Rate limit tracking: {tool: {"until": timestamp, "retries": count}}
    _rate_limits = {}
    
    # Tool availability cache: {tool: True/False}
    _tool_available = {}

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        """
        Initialize orchestrator with optional configuration.
        
        Args:
            config: Dict with keys:
                - gemini_cli_path: Path to gemini-cli executable
                - copilot_cli_path: Path to copilot executable (default: "copilot")
                - tool_preferences: {agent_name: "copilot"/"gemini"}
                - fallback_enabled: bool (default: True)
                - rate_limit_ttl: seconds (default: 3600)
                - max_retries: int (default: 2)
        """
        self.config = config or {}
        self.gemini_cli_path = self.config.get("gemini_cli_path", "gemini")
        self.copilot_cli_path = self.config.get("copilot_cli_path", "copilot")
        self.tool_preferences = self.config.get("tool_preferences", {})
        self.fallback_enabled = self.config.get("fallback_enabled", True)
        self.rate_limit_ttl = self.config.get("rate_limit_ttl", 3600)
        self.max_retries = self.config.get("max_retries", 2)

    def check_tool_available(self, tool: AIProvider) -> bool:
        """Check if a tool is available and not rate-limited."""
        # Check cache (5-minute validity)
        if tool.value in self._tool_available:
            cached_at = self._tool_available.get(f"{tool.value}_cached_at", 0)
            if time.time() - cached_at < 300:
                return self._tool_available[tool.value]

        # Check for rate limiting
        if tool.value in self._rate_limits:
            rate_info = self._rate_limits[tool.value]
            if time.time() < rate_info["until"]:
                logger.warning(
                    f"⏸️  {tool.value.upper()} is rate-limited until {rate_info['until']} "
                    f"(retries: {rate_info['retries']}/{self.max_retries})"
                )
                return False
            else:
                # Rate limit expired, reset
                del self._rate_limits[tool.value]

        # Check if tool command exists
        try:
            path = self.gemini_cli_path if tool == AIProvider.GEMINI else self.copilot_cli_path
            subprocess.run(
                [path, "--version"],
                capture_output=True,
                timeout=5,
                check=False
            )
            available = True
            logger.info(f"✅ {tool.value.upper()} available")
        except Exception as e:
            available = False
            logger.warning(f"⚠️  {tool.value.upper()} unavailable: {e}")

        # Cache result
        self._tool_available[tool.value] = available
        self._tool_available[f"{tool.value}_cached_at"] = time.time()
        return available

    def get_primary_tool(self, agent_name: Optional[str] = None) -> AIProvider:
        """
        Determines which tool should be primary for this agent.
        
        Strategy:
        - Copilot better for: code generation, complex reasoning, multi-file refactoring
        - Gemini better for: transcription, fast routing, content creation, simple analysis
        
        Args:
            agent_name: Optional agent name for config lookup
            
        Returns:
            Primary AIProvider to use
        """
        # Check explicit preference
        if agent_name and agent_name in self.tool_preferences:
            pref = self.tool_preferences[agent_name]
            return AIProvider.COPILOT if pref == "copilot" else AIProvider.GEMINI

        # Default: Copilot for agents, Gemini for utilities
        if agent_name:
            # Code-generation agents prefer Copilot
            code_agents = {
                "Tier2Lead", "BackendLead", "FrontendLead", "MobileLead",
                "Architect", "ProductDesigner", "Atlas", "ProjectLead"
            }
            if agent_name in code_agents:
                return AIProvider.COPILOT
            
            # QA/Content agents prefer Gemini for speed
            utility_agents = {"QAGuard", "Scribe", "Privacy", "OpsCommander"}
            if agent_name in utility_agents:
                return AIProvider.GEMINI

        # Default: Try Copilot first (assume availability)
        return AIProvider.COPILOT

    def get_fallback_tool(self, primary: AIProvider) -> Optional[AIProvider]:
        """
        Get fallback tool for the given primary.
        
        Args:
            primary: Primary tool that failed
            
        Returns:
            Fallback tool or None if not enabled
        """
        if not self.fallback_enabled:
            return None

        fallback = AIProvider.GEMINI if primary == AIProvider.COPILOT else AIProvider.COPILOT
        if self.check_tool_available(fallback):
            logger.info(f"🔄 Attempting fallback from {primary.value} → {fallback.value}")
            return fallback
        else:
            logger.error(f"❌ Fallback {fallback.value} unavailable")
            return None

    def record_rate_limit(self, tool: AIProvider, retry_count: int = 1):
        """Record rate limit for a tool."""
        self._rate_limits[tool.value] = {
            "until": time.time() + self.rate_limit_ttl,
            "retries": retry_count
        }
        logger.warning(f"⏸️  {tool.value.upper()} rate-limited for {self.rate_limit_ttl}s")

    def record_failure(self, tool: AIProvider):
        """Record tool failure for fallback escalation."""
        if tool.value not in self._rate_limits:
            self._tool_available[tool.value] = False
            logger.error(f"❌ {tool.value.upper()} marked unavailable")
        else:
            current = self._rate_limits[tool.value]
            current["retries"] += 1
            if current["retries"] >= self.max_retries:
                logger.error(f"❌ {tool.value.upper()} exceeded max retries, marking unavailable")
                self._tool_available[tool.value] = False

    # ========== AGENT INVOCATION ==========

    def invoke_agent(
        self,
        agent_prompt: str,
        workspace_dir: str,
        agents_dir: str,
        base_dir: str,
        issue_url: Optional[str] = None,
        agent_name: Optional[str] = None,
        use_gemini: bool = False
    ) -> Tuple[Optional[int], AIProvider]:
        """
        Invoke an agent using appropriate tool with fallback support.
        
        Args:
            agent_prompt: The prompt/instructions for the agent
            workspace_dir: Directory containing agent workspace
            agents_dir: Directory containing agents repo
            base_dir: Base directory for context
            issue_url: Optional GitHub issue URL (for logging)
            agent_name: Optional agent name (for tool selection)
            use_gemini: Force gemini (mostly for testing/experimentation)
            
        Returns:
            Tuple of (PID or None, tool_used: AIProvider)
            
        Raises:
            ToolUnavailableError: If all tools unavailable
        """
        primary = AIProvider.GEMINI if use_gemini else self.get_primary_tool(agent_name)
        fallback = self.get_fallback_tool(primary)

        # Try primary tool
        try:
            if primary == AIProvider.COPILOT:
                pid = self._invoke_copilot(agent_prompt, workspace_dir, agents_dir, base_dir)
                if pid:
                    return pid, AIProvider.COPILOT
            else:
                pid = self._invoke_gemini_agent(agent_prompt, workspace_dir, agents_dir, base_dir)
                if pid:
                    return pid, AIProvider.GEMINI
        except RateLimitedError:
            self.record_rate_limit(primary)
        except Exception as e:
            logger.error(f"❌ {primary.value} invocation failed: {e}")
            self.record_failure(primary)

        # Try fallback
        if fallback:
            try:
                if fallback == AIProvider.COPILOT:
                    pid = self._invoke_copilot(agent_prompt, workspace_dir, agents_dir, base_dir)
                    if pid:
                        logger.info(f"✅ Fallback {fallback.value} succeeded")
                        return pid, AIProvider.COPILOT
                else:
                    pid = self._invoke_gemini_agent(agent_prompt, workspace_dir, agents_dir, base_dir)
                    if pid:
                        logger.info(f"✅ Fallback {fallback.value} succeeded")
                        return pid, AIProvider.GEMINI
            except Exception as e:
                logger.error(f"❌ Fallback {fallback.value} also failed: {e}")
                self.record_failure(fallback)

        raise ToolUnavailableError(
            f"All AI tools unavailable. Primary: {primary.value}, Fallback: {fallback.value if fallback else 'None'}"
        )

    def _invoke_copilot(
        self,
        agent_prompt: str,
        workspace_dir: str,
        agents_dir: str,
        base_dir: str
    ) -> Optional[int]:
        """
        Invoke Copilot CLI agent.
        
        Returns:
            PID of launched process or None if failed
            
        Raises:
            RateLimitedError: If Copilot rate-limited
        """
        if not self.check_tool_available(AIProvider.COPILOT):
            raise ToolUnavailableError("Copilot not available")

        cmd = [
            self.copilot_cli_path,
            "-p", agent_prompt,
            "--add-dir", base_dir,
            "--add-dir", workspace_dir,
            "--add-dir", agents_dir,
            "--allow-all-tools"
        ]

        timestamp = time.strftime("%Y%m%d_%H%M%S")
        log_dir = os.path.join(workspace_dir, ".github", "tasks", "logs")
        os.makedirs(log_dir, exist_ok=True)
        log_path = os.path.join(log_dir, f"copilot_{timestamp}.log")

        logger.info(f"🤖 Launching Copilot CLI agent")
        logger.info(f"   Workspace: {workspace_dir}")
        logger.info(f"   Log: {log_path}")

        try:
            log_file = open(log_path, "w")
            process = subprocess.Popen(
                cmd,
                cwd=workspace_dir,
                stdout=log_file,
                stderr=subprocess.STDOUT
            )
            logger.info(f"🚀 Copilot launched (PID: {process.pid})")
            return process.pid
        except Exception as e:
            logger.error(f"❌ Copilot launch failed: {e}")
            raise

    def _invoke_gemini_agent(
        self,
        agent_prompt: str,
        workspace_dir: str,
        agents_dir: str,
        base_dir: str
    ) -> Optional[int]:
        """
        Invoke Gemini CLI agent (experimental).
        
        Returns:
            PID of launched process or None if failed
        """
        if not self.check_tool_available(AIProvider.GEMINI):
            raise ToolUnavailableError("Gemini CLI not available")

        cmd = [
            self.gemini_cli_path,
            "generate",
            "--prompt", agent_prompt,
            "--project-dir", workspace_dir,
            "--context-dir", agents_dir,
        ]

        timestamp = time.strftime("%Y%m%d_%H%M%S")
        log_dir = os.path.join(workspace_dir, ".github", "tasks", "logs")
        os.makedirs(log_dir, exist_ok=True)
        log_path = os.path.join(log_dir, f"gemini_{timestamp}.log")

        logger.info(f"🤖 Launching Gemini CLI agent")
        logger.info(f"   Workspace: {workspace_dir}")
        logger.info(f"   Log: {log_path}")

        try:
            log_file = open(log_path, "w")
            process = subprocess.Popen(
                cmd,
                cwd=workspace_dir,
                stdout=log_file,
                stderr=subprocess.STDOUT
            )
            logger.info(f"🚀 Gemini launched (PID: {process.pid})")
            return process.pid
        except Exception as e:
            logger.error(f"❌ Gemini launch failed: {e}")
            raise

    # ========== UTILITY OPERATIONS (Transcription, Routing, etc.) ==========

    def transcribe_audio_cli(self, audio_file_path: str) -> Optional[str]:
        """
        Transcribe audio file using gemini-cli with fallback to copilot-cli.
        
        Args:
            audio_file_path: Path to audio file (.ogg, .mp3, .wav, .m4a)
            
        Returns:
            Transcribed text or None if transcription failed (after fallback)
        """
        primary = AIProvider.GEMINI  # Gemini better for audio
        fallback = self.get_fallback_tool(primary)

        # Try primary
        try:
            text = self._transcribe_with_gemini_cli(audio_file_path)
            if text:
                logger.info(f"✅ Transcription successful with {primary.value}")
                return text
        except RateLimitedError:
            self.record_rate_limit(primary)
        except Exception as e:
            logger.warning(f"⚠️  {primary.value} transcription failed: {e}")

        # Try fallback
        if fallback:
            try:
                text = self._transcribe_with_copilot_cli(audio_file_path)
                if text:
                    logger.info(f"✅ Fallback transcription succeeded with {fallback.value}")
                    return text
            except Exception as e:
                logger.error(f"❌ Fallback transcription also failed: {e}")

        logger.error(f"❌ All transcription tools failed")
        return None

    def _transcribe_with_gemini_cli(self, audio_file_path: str) -> Optional[str]:
        """Transcribe using gemini-cli."""
        if not self.check_tool_available(AIProvider.GEMINI):
            raise ToolUnavailableError("Gemini CLI not available")

        if not os.path.exists(audio_file_path):
            raise ValueError(f"Audio file not found: {audio_file_path}")

        logger.info(f"🎧 Transcribing with Gemini: {audio_file_path}")

        try:
            result = subprocess.run(
                [
                    self.gemini_cli_path,
                    "generate",
                    "--prompt", "Transcribe this audio exactly. Return ONLY the text.",
                    "--file", audio_file_path
                ],
                capture_output=True,
                text=True,
                timeout=60
            )

            if result.returncode != 0:
                if "rate" in result.stderr.lower() or "quota" in result.stderr.lower():
                    raise RateLimitedError(f"Gemini rate-limited: {result.stderr}")
                raise Exception(f"Gemini error: {result.stderr}")

            text = result.stdout.strip()
            if text:
                return text
            else:
                raise Exception("Gemini returned empty transcription")

        except subprocess.TimeoutExpired:
            raise Exception("Gemini transcription timed out (>60s)")

    def _transcribe_with_copilot_cli(self, audio_file_path: str) -> Optional[str]:
        """Transcribe using copilot-cli (fallback)."""
        if not self.check_tool_available(AIProvider.COPILOT):
            raise ToolUnavailableError("Copilot CLI not available")

        if not os.path.exists(audio_file_path):
            raise ValueError(f"Audio file not found: {audio_file_path}")

        logger.info(f"🎧 Transcribing with Copilot (fallback): {audio_file_path}")

        try:
            result = subprocess.run(
                [
                    self.copilot_cli_path,
                    "-p", "Transcribe this audio exactly. Return ONLY the text.",
                    "--add-file", audio_file_path
                ],
                capture_output=True,
                text=True,
                timeout=60
            )

            if result.returncode != 0:
                raise Exception(f"Copilot error: {result.stderr}")

            text = result.stdout.strip()
            if text:
                return text
            else:
                raise Exception("Copilot returned empty transcription")

        except subprocess.TimeoutExpired:
            raise Exception("Copilot transcription timed out (>60s)")

    def run_text_to_speech_analysis(
        self,
        text: str,
        task: str = "classify",
        **kwargs
    ) -> Dict[str, Any]:
        """
        Run text analysis (classification, routing, generation) with fallback.
        
        Args:
            text: Input text to analyze
            task: "classify", "route", "generate_name", etc.
            **kwargs: Additional context (project_name, etc.)
            
        Returns:
            Dict with analysis result
        """
        primary = AIProvider.GEMINI  # Gemini better for quick analysis
        fallback = self.get_fallback_tool(primary)

        result = None

        # Try primary
        try:
            result = self._run_gemini_cli_analysis(text, task, **kwargs)
            if result:
                return result
        except RateLimitedError:
            self.record_rate_limit(primary)
        except Exception as e:
            logger.warning(f"⚠️  {primary.value} analysis failed: {e}")

        # Try fallback
        if fallback:
            try:
                result = self._run_copilot_analysis(text, task, **kwargs)
                if result:
                    logger.info(f"✅ Fallback analysis succeeded with {fallback.value}")
                    return result
            except Exception as e:
                logger.error(f"❌ Fallback analysis also failed: {e}")

        # Last resort: return default
        logger.warning(f"⚠️  All tools failed for {task}, returning default")
        return self._get_default_analysis_result(task, **kwargs)

    def _run_gemini_cli_analysis(
        self,
        text: str,
        task: str,
        **kwargs
    ) -> Dict[str, Any]:
        """Run analysis using gemini-cli."""
        if not self.check_tool_available(AIProvider.GEMINI):
            raise ToolUnavailableError("Gemini CLI not available")

        prompt = self._build_analysis_prompt(text, task, **kwargs)

        # Write prompt to temp file
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write(prompt)
            temp_prompt_file = f.name

        try:
            result = subprocess.run(
                [self.gemini_cli_path, "generate", "--prompt", prompt],
                capture_output=True,
                text=True,
                timeout=30
            )

            if result.returncode != 0:
                if "rate" in result.stderr.lower() or "quota" in result.stderr.lower():
                    raise RateLimitedError(f"Gemini rate-limited: {result.stderr}")
                raise Exception(f"Gemini error: {result.stderr}")

            return self._parse_analysis_result(result.stdout, task)

        finally:
            if os.path.exists(temp_prompt_file):
                os.remove(temp_prompt_file)

    def _run_copilot_analysis(
        self,
        text: str,
        task: str,
        **kwargs
    ) -> Dict[str, Any]:
        """Run analysis using copilot-cli (via interactive prompt)."""
        if not self.check_tool_available(AIProvider.COPILOT):
            raise ToolUnavailableError("Copilot CLI not available")

        prompt = self._build_analysis_prompt(text, task, **kwargs)

        try:
            result = subprocess.run(
                [self.copilot_cli_path, "-p", prompt],
                capture_output=True,
                text=True,
                timeout=30
            )

            if result.returncode != 0:
                raise Exception(f"Copilot error: {result.stderr}")

            return self._parse_analysis_result(result.stdout, task)

        except subprocess.TimeoutExpired:
            raise Exception("Copilot analysis timed out")

    def _build_analysis_prompt(self, text: str, task: str, **kwargs) -> str:
        """Build prompt for analysis task."""
        if task == "classify":
            projects = kwargs.get("projects", [])
            types = kwargs.get("types", [])
            return f"""Classify this task:
Text: {text[:500]}

1. Map to project (one of: {", ".join(projects)}). Use key format.
2. Classify type (one of: {", ".join(types)}).
3. Generate concise issue name (3-6 words, kebab-case).
4. Return JSON: {{"project": "key", "type": "type_key", "issue_name": "name"}}

Return ONLY valid JSON."""

        elif task == "route":
            return f"""Route this task to the best agent:
{text[:500]}

1. Identify primary work type (coding, design, testing, ops, content).
2. Suggest best agent.
3. Rate confidence 0-100.
4. Return JSON: {{"agent": "name", "type": "work_type", "confidence": 85}}

Return ONLY valid JSON."""

        elif task == "generate_name":
            project = kwargs.get("project_name", "")
            return f"""Generate a concise issue name (3-6 words, kebab-case):
{text[:300]}
Project: {project}

Return ONLY the name, no quotes."""

        else:
            return text

    def _parse_analysis_result(self, output: str, task: str) -> Dict[str, Any]:
        """Parse CLI output into structured result."""
        output = output.strip()

        try:
            # Try JSON parsing first
            if output.startswith("{"):
                return json.loads(output)
            # Otherwise return as text result
            return {"text": output}
        except json.JSONDecodeError:
            logger.warning(f"Failed to parse {task} result as JSON: {output[:100]}")
            return {"text": output, "parse_error": True}

    def _get_default_analysis_result(self, task: str, **kwargs) -> Dict[str, Any]:
        """Return sensible default when AI tools fail."""
        if task == "classify":
            return {
                "project": kwargs.get("projects", ["case-italia"])[0],
                "type": kwargs.get("types", ["feature"])[0],
                "issue_name": "generic-task"
            }
        elif task == "route":
            return {
                "agent": "ProjectLead",
                "type": "routing",
                "confidence": 0
            }
        elif task == "generate_name":
            text = kwargs.get("text", "")
            words = text.split()[:3]
            return {"text": "-".join(words).lower()}
        else:
            return {}


# Global orchestrator instance
_orchestrator: Optional[AIOrchestrator] = None


def get_orchestrator(config: Optional[Dict[str, Any]] = None) -> AIOrchestrator:
    """Get or create global orchestrator instance."""
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = AIOrchestrator(config)
    return _orchestrator


def reset_orchestrator():
    """Reset global orchestrator (for testing)."""
    global _orchestrator
    _orchestrator = None
