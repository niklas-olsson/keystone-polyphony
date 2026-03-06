import os
import json
from typing import Dict, Any, Optional


class Architect:
    def __init__(self, api_key: Optional[str] = None, model: str = None):
        self.api_key = api_key or os.getenv("DUCKY_API_KEY")
        self.model = model or os.getenv("DUCKY_MODEL", "gpt-4o")
        self.provider = "openai"
        self.client = None
        self.google_model = None

        # Check for explicit provider override
        provider_env = os.getenv("DUCKY_PROVIDER")
        if provider_env:
            self.provider = provider_env.lower()
        # Heuristics if provider not explicit
        elif self.model.startswith("ollama:"):
            self.provider = "ollama"
            self.model = self.model.split(":", 1)[1]
        elif self.api_key:
            # Simple heuristic to detect provider
            if self.model.startswith("gemini") or (
                self.api_key.startswith("AIza") and len(self.api_key) > 30
            ):
                self.provider = "google"
            elif self.model.startswith("claude") or self.api_key.startswith("sk-ant"):
                self.provider = "anthropic"
            else:
                # Default to openai if api_key present and no other match
                self.provider = "openai"

        # Initialize based on provider
        if self.provider == "ollama":
            try:
                import ollama

                self.client = ollama.AsyncClient()
            except ImportError:
                print(
                    "Warning: ollama package not installed. Architect disabled for Ollama."
                )

        elif self.provider == "google":
            if self.api_key:
                try:
                    import google.generativeai as genai

                    genai.configure(api_key=self.api_key)
                    self.google_model = genai.GenerativeModel(self.model)
                except ImportError:
                    print(
                        "Warning: google-generativeai package not installed. Architect disabled for Gemini."
                    )
            else:
                print("Warning: API key missing for Google provider.")

        elif self.provider == "anthropic":
            if self.api_key:
                try:
                    from anthropic import AsyncAnthropic

                    self.client = AsyncAnthropic(api_key=self.api_key)
                    # If model not explicitly set to a claude model (e.g. still default gpt-4o),
                    # switch default
                    if not self.model or "claude" not in self.model:
                        self.model = "claude-3-5-sonnet-20240620"
                except ImportError:
                    print(
                        "Warning: anthropic package not installed. Architect disabled for Anthropic."
                    )
            else:
                print("Warning: API key missing for Anthropic provider.")

        elif self.provider == "openai":
            # OpenAI / Default
            if self.api_key:
                try:
                    from openai import AsyncOpenAI

                    self.client = AsyncOpenAI(api_key=self.api_key)
                except ImportError:
                    print(
                        "Warning: openai package not installed. Architect disabled for OpenAI."
                    )
            else:
                # No API key, no client for OpenAI
                pass

    async def consult(self, swarm_state: Dict[str, Any]) -> str:
        """
        Consults the external 'Rubber Ducky' (Architect) to refine the plan.
        """
        prompt = f"""
        You are the System Architect for a swarm of autonomous coding agents (Jules).
        Your goal is to prevent hallucinations and coordination failures by providing a clear, high-level plan.

        Current Swarm State (Liminal Space):
        {json.dumps(swarm_state, indent=2)}

        Task:
        1. Analyze the current state of thoughts and locks (batons).
        2. Identify any conflicts, missing tasks, or potential deadlocks (e.g. locks held for too long).
        3. Suggest releasing locks if tasks appear complete but the lock is still held.
        4. Generate a refined Backlog of the next 3-5 critical tasks, prioritized by dependency.
        5. If a high-priority task is pending and an agent is 'idle' with matching capabilities, you MAY issue a direct command to that agent.
        6. Output the result as a JSON object with:
           - 'backlog': List of tasks.
           - 'commands': List of objects with {{'target': 'node_id', 'command': 'string_or_dict', 'capabilities': 'list'}}.
           - 'advisories': Optional warnings.
        """

        if self.provider == "google":
            return await self._consult_google(prompt)
        elif self.provider == "anthropic":
            return await self._consult_anthropic(prompt)
        elif self.provider == "ollama":
            return await self._consult_ollama(prompt)
        else:
            return await self._consult_openai(prompt)

    async def deduplicate_backlog(self, issues: Dict[str, str]) -> str:
        """
        Analyzes a backlog of issues and identifies duplicates.
        Returns a JSON structure mapping primary issues to their duplicates.
        """
        prompt = f"""
        You are an expert technical product manager.
        Analyze the following backlog of issues for duplicates or redundant findings.

        Backlog:
        {json.dumps(issues, indent=2)}

        Task:
        1. Identify clusters of issues that describe the same underlying problem or feature request.
        2. For each cluster, select the most comprehensive issue as the 'primary'.
        3. List the other issues in the cluster as 'duplicates'.
        4. If an issue is unique, do not include it in the 'duplicates' list.
        5. Return a JSON object where keys are the filenames of the primary issues, and values are lists of filenames
           of duplicates to be merged into the primary.
           Example: {{ "issue1.md": ["issue2.md", "issue3.md"] }}
           If no duplicates are found, return an empty object {{}}.
        """

        if self.provider == "google":
            return await self._consult_google(prompt)
        elif self.provider == "anthropic":
            return await self._consult_anthropic(prompt)
        elif self.provider == "ollama":
            return await self._consult_ollama(prompt)
        else:
            return await self._consult_openai(prompt)

    async def refine_issue(self, issue_content: str) -> str:
        """
        Refines a raw issue description into a structured format.
        """
        prompt = f"""
        You are an expert software architect and product manager.
        Your task is to refine the following raw issue description into a well-structured, ready-for-work specification.

        Raw Issue Content:
        ---
        {issue_content}
        ---

        Requirements for the Refined Issue:
        1. **Human Interaction Story**: A clear narrative of how a user interacts with the feature.
        2. **Technical Approach**: High-level architectural decisions, data models, and key components.
        3. **BDD Feature File**: A complete Cucumber/Gherkin .feature section.
        4. **Verification Plan**: Step-by-step instructions to manually verify the feature.
        5. **Self-Contained**: Explicitly list prerequisites and mark them as blocking dependencies if known.
        6. **Surgical Scope**: Ensure the issue covers one specific concern.
        7. **Format**: Return ONLY the markdown content for the new issue body. Do not include JSON.

        Refined Output:
        """

        if self.provider == "google":
            return await self._refine_google(prompt)
        elif self.provider == "anthropic":
            return await self._refine_anthropic(prompt)
        elif self.provider == "ollama":
            return await self._refine_ollama(prompt)
        else:
            return await self._refine_openai(prompt)

    async def review_app(self, codebase_metrics: Dict[str, Any]) -> str:
        """
        Conducts a periodic review of the application based on aggregated metrics.
        Returns a structured Markdown report.
        """
        prompt = f"""
        You are an expert Principal Engineer and System Reviewer.
        Your task is to analyze the following aggregated metrics and recent activities
        of our system to generate a comprehensive, structured Markdown review report.

        Aggregated Data:
        {json.dumps(codebase_metrics, indent=2)}

        Requirements for the Review Report:
        1. **Executive Summary**: A brief overview of the system's current health and recent progress.
        2. **Technical Debt & Bottlenecks**: Identify any lingering issues or architectural bottlenecks based on the TODOs and recent discoveries.
        3. **Unhandled Edge Cases**: Highlight any potential edge cases or missing test coverage implied by the data.
        4. **Proposed Next Steps**: A prioritized list of actionable recommendations for the swarm or human engineers.
        5. **Format**: Return ONLY valid Markdown content. Do not wrap in JSON.

        Generate the Markdown Review Report:
        """

        if self.provider == "google":
            return await self._refine_google(prompt)
        elif self.provider == "anthropic":
            return await self._refine_anthropic(prompt)
        elif self.provider == "ollama":
            return await self._refine_ollama(prompt)
        else:
            return await self._refine_openai(prompt)

    @property
    def is_configured(self) -> bool:
        return bool(self.client or self.google_model)

    async def _consult_openai(self, prompt: str) -> str:
        if not self.client:
            return "Architect not configured (missing API key or openai package)."

        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": "You are a precise technical architect.",
                    },
                    {"role": "user", "content": prompt},
                ],
                response_format={"type": "json_object"},
            )
            return response.choices[0].message.content
        except Exception as e:
            return f"Error consulting architect (OpenAI): {str(e)}"

    async def _refine_openai(self, prompt: str) -> str:
        if not self.client:
            return "Architect not configured (missing API key or openai package)."
        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": "You are a precise technical architect.",
                    },
                    {"role": "user", "content": prompt},
                ],
            )
            return response.choices[0].message.content
        except Exception as e:
            return f"Error refining issue (OpenAI): {str(e)}"

    async def _consult_google(self, prompt: str) -> str:
        if not self.google_model:
            return "Architect not configured (missing API key or google-generativeai package)."

        try:
            response = await self.google_model.generate_content_async(
                contents=[prompt],
                generation_config={"response_mime_type": "application/json"},
            )
            return response.text
        except Exception as e:
            return f"Error consulting architect (Gemini): {str(e)}"

    async def _refine_google(self, prompt: str) -> str:
        if not self.google_model:
            return "Architect not configured (missing API key or google-generativeai package)."
        try:
            response = await self.google_model.generate_content_async(contents=[prompt])
            return response.text
        except Exception as e:
            return f"Error refining issue (Gemini): {str(e)}"

    async def _consult_anthropic(self, prompt: str) -> str:
        if not self.client:
            return "Architect not configured (missing API key or anthropic package)."

        try:
            response = await self.client.messages.create(
                model=self.model,
                max_tokens=1024,
                system="You are a precise technical architect. Output only valid JSON.",
                messages=[{"role": "user", "content": prompt}],
            )
            return response.content[0].text
        except Exception as e:
            return f"Error consulting architect (Anthropic): {str(e)}"

    async def _refine_anthropic(self, prompt: str) -> str:
        if not self.client:
            return "Architect not configured (missing API key or anthropic package)."
        try:
            response = await self.client.messages.create(
                model=self.model,
                max_tokens=2048,
                system="You are a precise technical architect.",
                messages=[{"role": "user", "content": prompt}],
            )
            return response.content[0].text
        except Exception as e:
            return f"Error refining issue (Anthropic): {str(e)}"

    async def _consult_ollama(self, prompt: str) -> str:
        if not self.client:
            return "Architect not configured (missing ollama package)."
        try:
            response = await self.client.chat(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": "You are a precise technical architect. Output only valid JSON.",
                    },
                    {"role": "user", "content": prompt},
                ],
                format="json",
            )
            return response["message"]["content"]
        except Exception as e:
            return f"Error consulting architect (Ollama): {str(e)}"

    async def _refine_ollama(self, prompt: str) -> str:
        if not self.client:
            return "Architect not configured (missing ollama package)."
        try:
            response = await self.client.chat(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": "You are a precise technical architect.",
                    },
                    {"role": "user", "content": prompt},
                ],
            )
            return response["message"]["content"]
        except Exception as e:
            return f"Error refining issue (Ollama): {str(e)}"
