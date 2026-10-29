class InfraAgent:
    """Multi-turn infrastructure agent backed by Gemini.

    The runtime will be LangChain ``create_agent`` (a LangGraph under the hood)
    with ``ChatGoogleGenerativeAI`` from ``langchain-google-genai``. The model
    comes from ``gemini_model`` (default ``gemini-4-argon``; ``gemini-3.8-flash``
    is the lighter alternative). When the client is wired, generation requests
    must set ``store=false`` so paid Gemini accounts do not retain prompt text.

    Planned tools are writing ``.tf`` files, reading the workspace, and running
    ``terraform validate`` / ``plan``. ``apply`` and ``destroy`` go through
    ``HumanInTheLoopMiddleware`` and run only after the user approves that plan.
    """
