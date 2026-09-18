from enum import Enum
import ipaddress
import re

from pydantic import BaseModel, Discriminator, Field, SecretStr, field_validator, model_validator
from typing import Annotated, Literal, List, Optional, Union


# ==================== Enums ====================

class OutputAction(str, Enum):
    """Determines the response format after actions are executed."""
    text = "text"
    html = "html"
    screenshot = "screenshot"
    screenshot_full = "screenshot_full"


# ==================== API Request Models ====================

class ConnectBrowserRequest(BaseModel):
    """Connect to a remote browser via its CDP WebSocket URL."""
    browser_id: str = Field(..., description="Unique identifier for this browser connection")
    ws_url: str = Field(..., description="CDP WebSocket URL (e.g. ws://10.0.0.5:34567/devtools/browser/abc-def)")


_SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_HOSTNAME_PATTERN = re.compile(
    r"^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)(?:\.(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?))*$"
)


def validate_session_id_value(value: str) -> str:
    if value in {".", ".."} or _SESSION_ID_PATTERN.fullmatch(value) is None:
        raise ValueError(
            "session_id must be 1-128 ASCII letters, digits, dots, underscores, or hyphens and cannot contain paths"
        )
    return value


class ProxyConfig(BaseModel):
    """Validated proxy settings. Credentials are write-only API input."""

    model_config = {"extra": "forbid"}

    type: Literal["http", "https", "socks5"]
    host: str = Field(..., min_length=1, max_length=253)
    port: int = Field(..., ge=1, le=65535)
    username: Optional[SecretStr] = None
    password: Optional[SecretStr] = None
    bypass: Optional[str] = Field(default=None, max_length=2048)

    @field_validator("host")
    @classmethod
    def validate_host(cls, value: str) -> str:
        if value != value.strip() or any(char in value for char in "/@?#"):
            raise ValueError("host must not contain a scheme, credentials, path, query, or fragment")
        candidate = value[1:-1] if value.startswith("[") and value.endswith("]") else value
        try:
            ipaddress.ip_address(candidate)
        except ValueError:
            if _HOSTNAME_PATTERN.fullmatch(candidate) is None:
                raise ValueError("host must be a valid IP address or DNS hostname")
        return candidate

    @field_validator("bypass")
    @classmethod
    def validate_bypass(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and (value != value.strip() or any(ord(char) < 32 for char in value)):
            raise ValueError("bypass must not contain surrounding whitespace or control characters")
        return value

    def to_record(self) -> dict:
        return {
            "type": self.type,
            "host": self.host,
            "port": self.port,
            "username": self.username.get_secret_value() if self.username else None,
            "password": self.password.get_secret_value() if self.password else None,
            "bypass": self.bypass,
        }


class SessionRegistrationRequest(BaseModel):
    model_config = {"extra": "forbid"}

    session_id: str
    proxy: ProxyConfig
    browser_id: Optional[str] = Field(default=None, min_length=1, max_length=128)

    @field_validator("session_id")
    @classmethod
    def validate_session_id(cls, value: str) -> str:
        return validate_session_id_value(value)


class RegisterSessionsRequest(BaseModel):
    model_config = {"extra": "forbid"}

    sessions: List[SessionRegistrationRequest] = Field(..., min_length=1, max_length=100)
    replace: bool = False

    @model_validator(mode="after")
    def unique_session_ids(self):
        session_ids = [item.session_id for item in self.sessions]
        if len(session_ids) != len(set(session_ids)):
            raise ValueError("sessions must contain unique session_id values")
        return self


class StartSessionRequest(BaseModel):
    """Start a legacy page or activate a durable, dedicated proxy session."""

    browser_id: Optional[str] = Field(
        default=None, description="Browser to create session in. Uses first connected browser if omitted.",
    )
    session_id: Optional[str] = Field(
        default=None, description="Stable registered session identifier for a dedicated context.",
    )
    proxy: Optional[ProxyConfig] = Field(
        default=None, description="Proxy settings used when registering a stable session.",
    )

    @field_validator("session_id")
    @classmethod
    def validate_session_id(cls, value: Optional[str]) -> Optional[str]:
        return validate_session_id_value(value) if value is not None else None

    @model_validator(mode="after")
    def proxy_requires_stable_session(self):
        if self.proxy is not None and self.session_id is None:
            raise ValueError("proxy requires a stable session_id")
        return self


class SearchRequest(BaseModel):
    """Google search with structured result parsing and optional wiki panel extraction."""
    query: str = Field(..., description="The search query string")
    count: int = Field(default=5, description="Maximum number of search results")
    idle: float = Field(default=3.0, description="Idle time in seconds after page loads before parsing results")
    max_pages: int = Field(default=10, ge=1, description="Maximum number of Google result pages to paginate through")


class SearchHintsRequest(BaseModel):
    """Get Google autocomplete suggestions (search hints) for a query."""
    query: str = Field(..., description="The search query to get hints for")
    idle: float = Field(default=1.0, description="Idle time in seconds after page loads")
    wait: float = Field(default=1.5, description="Time in seconds to wait for suggestions to appear after typing")


class LentaBootstrapRequest(BaseModel):
    """Bootstrap one session-scoped Lenta storefront context."""

    timeout: float = Field(default=30000, gt=0, le=120000)


class LentaItemRequest(BaseModel):
    """Fetch one canonical Lenta item using a bootstrapped browser session."""

    url: str = Field(..., description="Canonical lenta.com /product/...-<id>/ URL")
    timeout: float = Field(default=30000, gt=0, le=120000)

    @model_validator(mode="after")
    def validate_product_url(self):
        import re
        from urllib.parse import urlsplit

        parsed = urlsplit(self.url)
        if parsed.scheme != "https" or parsed.hostname not in {"lenta.com", "www.lenta.com"}:
            raise ValueError("url must be an HTTPS lenta.com product URL")
        match = re.fullmatch(r"/product/.+-(\d+)/?", parsed.path)
        if match is None or parsed.query or parsed.fragment:
            raise ValueError("url must be a canonical /product/...-<id>/ URL")
        return self

    @property
    def product_id(self) -> str:
        import re
        from urllib.parse import urlsplit

        match = re.fullmatch(r"/product/.+-(\d+)/?", urlsplit(self.url).path)
        return match.group(1)


class UtkonosBootstrapRequest(BaseModel):
    """Bootstrap one session-scoped Utkonos storefront context."""

    timeout: float = Field(default=30000, gt=0, le=120000)


class UtkonosListingRequest(BaseModel):
    """Fetch one bounded Utkonos catalog page."""

    category_id: str = Field(..., pattern=r"^\d+$")
    limit: int = Field(default=40, ge=1, le=40)
    offset: int = Field(default=0, ge=0)


class UtkonosItemRequest(BaseModel):
    """Fetch one canonical Utkonos item using a bootstrapped session."""

    url: str = Field(..., description="Canonical utkonos.ru /item/<sku>/ URL")
    timeout: float = Field(default=30000, gt=0, le=120000)

    @model_validator(mode="after")
    def validate_product_url(self):
        import re
        from urllib.parse import urlsplit

        parsed = urlsplit(self.url)
        if parsed.scheme != "https" or parsed.hostname not in {"utkonos.ru", "www.utkonos.ru"}:
            raise ValueError("url must be an HTTPS utkonos.ru product URL")
        match = re.fullmatch(r"/item/(\d+)/?", parsed.path)
        if match is None or parsed.query or parsed.fragment:
            raise ValueError("url must be a canonical /item/<sku>/ URL")
        return self

    @property
    def product_id(self) -> str:
        import re
        from urllib.parse import urlsplit

        match = re.fullmatch(r"/item/(\d+)/?", urlsplit(self.url).path)
        return match.group(1)


class ContentRequest(BaseModel):
    """
    Get page content with automatic scrolling.

    Shortcut for: navigate → idle → scroll_to_bottom → output.
    The scroll timeout is calculated as ``steps × step_delay``.
    """
    url: Optional[str] = Field(default=None, description="URL to navigate to. If not provided, uses current page.")
    wait_until: Literal["commit", "domcontentloaded", "load", "networkidle"] = Field(
        default="commit", description="Navigation wait condition",
    )
    timeout: float = Field(default=30000, description="Navigation timeout in milliseconds")
    idle: float = Field(default=2, description="Idle time in seconds after page loads")
    output_action: OutputAction = Field(
        default=OutputAction.text,
        description="Output format: 'text', 'html', 'screenshot', or 'screenshot_full'",
    )
    steps: int = Field(default=8, ge=0, description="Number of scroll steps (0 = no scroll, 4-12 recommended)")
    step_delay: float = Field(default=1.5, description="Delay between scroll steps in seconds")
    step_pixels: int = Field(default=1500, description="Pixels to scroll per step")


# ==================== Action Models ====================

class Action(BaseModel):
    """Base action model. All specific actions inherit from this."""
    action: str = Field(..., description="Action type identifier")


class ScrollAction(Action):
    """Scroll the page by specified delta."""
    action: Literal["scroll"] = Field(default="scroll")
    x: float = Field(default=0, description="Horizontal scroll delta")
    y: float = Field(default=0, description="Vertical scroll delta (positive = down)")


class ScrollToBottomAction(Action):
    """Scroll to bottom in steps until timeout is reached (duration-based)."""
    action: Literal["scroll_to_bottom"] = Field(default="scroll_to_bottom")
    step_pixels: int = Field(default=500, description="Pixels per scroll step")
    step_delay: float = Field(default=0.2, description="Delay between steps in seconds")
    timeout: float = Field(default=30.0, description="Maximum scroll time in seconds")


class MoveAction(Action):
    """Move mouse cursor to coordinates."""
    action: Literal["move"] = Field(default="move")
    x: float = Field(..., description="X coordinate")
    y: float = Field(..., description="Y coordinate")
    steps: int = Field(default=10, description="Intermediate steps for smooth movement")


class MouseClickAction(Action):
    """Click at specific x,y coordinates on the page."""
    action: Literal["mouse_click"] = Field(default="mouse_click")
    x: float = Field(..., description="X coordinate")
    y: float = Field(..., description="Y coordinate")
    button: Literal["left", "right", "middle"] = Field(default="left")
    click_count: int = Field(default=1, description="Number of clicks (2 for double-click)")
    delay: float = Field(default=0, description="Delay between mousedown and mouseup in ms")


class IdleAction(Action):
    """Wait for a specified duration."""
    action: Literal["idle"] = Field(default="idle")
    duration: float = Field(..., description="Duration to wait in seconds")


class LoginAction(Action):
    """Set HTTP Basic Authentication credentials for the browser context."""
    action: Literal["login"] = Field(default="login")
    username: str = Field(..., description="HTTP auth username")
    password: str = Field(..., description="HTTP auth password")


class ClickArgs(BaseModel):
    """Arguments for clicking matched elements."""
    nth: Optional[int] = Field(default=0, description="Which element: 0=first, -1=last, null=all")


class FillArgs(BaseModel):
    """Arguments for filling matched input elements."""
    value: str = Field(..., description="Value to fill into the input element(s)")
    nth: Optional[int] = Field(default=0, description="Which element: 0=first, -1=last, null=all")


class RemoveArgs(BaseModel):
    """Arguments for removing matched elements from the DOM."""
    nth: Optional[int] = Field(default=0, description="Which element: 0=first, -1=last, null=all")


class SelectAction(Action):
    """
    Perform a DOM action on elements matching a CSS or XPath selector.

    One selector = one operation. Chain multiple selectors for multi-step workflows.
    The ``args`` field must match the ``do`` action type.

    Examples::

        {"action": "selector", "type": "css", "value": ".ad", "do": "remove", "args": {"nth": null}}
        {"action": "selector", "type": "css", "value": "input.email", "do": "fill", "args": {"value": "me@x.com"}}
        {"action": "selector", "value": "button.submit", "do": "click"}
    """
    action: Literal["selector"] = Field(default="selector")
    type: Literal["css", "xml"] = Field(default="css", description="Selector type: css or xml (xpath)")
    value: str = Field(..., description="The selector string")
    do: Literal["click", "fill", "remove"] = Field(..., description="Action to perform on matched elements")
    args: Union[ClickArgs, FillArgs, RemoveArgs] = Field(
        default_factory=ClickArgs,
        description="Arguments for the action. Must match 'do' type: ClickArgs, FillArgs, or RemoveArgs.",
    )

    @model_validator(mode="before")
    @classmethod
    def coerce_args_to_type(cls, data):
        """Parse ``args`` dict into the correct type based on ``do``."""
        if isinstance(data, dict):
            do = data.get("do")
            args = data.get("args", {})
            if isinstance(args, dict):
                type_map = {"click": ClickArgs, "fill": FillArgs, "remove": RemoveArgs}
                if do in type_map:
                    data["args"] = type_map[do](**args)
        return data


# ==================== Discriminated Union ====================

InteractAction = Annotated[
    Union[
        ScrollAction, ScrollToBottomAction, MoveAction,
        MouseClickAction, IdleAction, LoginAction, SelectAction,
    ],
    Discriminator("action"),
]


# ==================== Compound Request Models ====================

class InteractRequest(BaseModel):
    """
    Unified endpoint for page interactions.

    1. Navigate to ``url`` (if provided) and wait ``idle`` seconds.
    2. Execute all ``actions`` in order (scroll, click, move, idle, login, selector).
    3. Return result based on ``output_action``: text / html / screenshot / screenshot_full.

    Available actions: scroll, scroll_to_bottom, move, mouse_click, idle, login, selector.

    Examples::

        {"output_action": "screenshot_full"}
        {"url": "https://example.com", "actions": [{"action": "scroll_to_bottom", "timeout": 10}], "output_action": "html"}
        {"actions": [{"action": "selector", "type": "css", "value": ".ad", "do": "remove", "args": {"nth": null}}], "output_action": "text"}
        {"actions": [{"action": "selector", "value": "input", "do": "fill", "args": {"value": "hello"}}, {"action": "selector", "value": "button", "do": "click"}], "output_action": "screenshot"}
    """
    url: Optional[str] = Field(default=None, description="URL to navigate to. If not provided, uses current page.")
    wait_until: Literal["commit", "domcontentloaded", "load", "networkidle"] = Field(default="commit")
    timeout: float = Field(default=30000, description="Navigation timeout in milliseconds")
    idle: float = Field(default=0, description="Idle time in seconds after page loads")
    actions: List[InteractAction] = Field(
        default_factory=list,
        description="List of actions to perform in order (scroll, click, move, idle, login, selector)",
    )
    output_action: OutputAction = Field(
        default=OutputAction.text,
        description="Output format: 'text', 'html', 'screenshot', or 'screenshot_full'",
    )


# ==================== Attribute Endpoint Models ====================

class AttributeSelector(BaseModel):
    """
    A single selector for extracting data from the page HTML.

    - **selector**: CSS selector string (or XPath if ``type`` is ``"xpath"``).
    - **attribute**: If set, extract this attribute from each matched element.
      If ``None``, extract the text content of each matched element.

    Examples::

        {"name": "links", "selector": "a.product-link", "attribute": "href"}
        {"name": "titles", "selector": "h2.title"}
        {"name": "images", "selector": "img.thumb", "attribute": "src"}
        {"name": "header", "selector": "//h1", "type": "xpath"}
    """
    name: str = Field(..., description="Identifier for this selector result")
    selector: str = Field(..., description="CSS or XPath selector string")
    type: Literal["css", "xpath"] = Field(
        default="css", description="Selector type: css or xpath",
    )
    attribute: Optional[str] = Field(
        default=None,
        description="Attribute to extract (e.g. 'href', 'src'). If null, extracts text content.",
    )


class AttributeRequest(BaseModel):
    """
    Extract structured data from a page using CSS or XPath selectors.

    Works like ``/content`` (navigate → idle → scroll) but instead of returning
    the full page, it uses BeautifulSoup / lxml to efficiently extract specific
    elements or attributes defined by the ``selectors`` list.

    Returns a JSON list of ``{name, values}`` objects.

    Example request::

        {
            "url": "https://example.com/products",
            "selectors": [
                {"name": "titles",  "selector": "h2.product-title"},
                {"name": "prices",  "selector": "span.price"},
                {"name": "links",   "selector": "a.product-link", "attribute": "href"},
                {"name": "header",  "selector": "//h1", "type": "xpath"}
            ]
        }
    """
    url: Optional[str] = Field(default=None, description="URL to navigate to. If not provided, uses current page.")
    wait_until: Literal["commit", "domcontentloaded", "load", "networkidle"] = Field(
        default="commit", description="Navigation wait condition",
    )
    timeout: float = Field(default=30000, description="Navigation timeout in milliseconds")
    idle: float = Field(default=2, description="Idle time in seconds after page loads")
    steps: int = Field(default=8, ge=0, description="Number of scroll steps (0 = no scroll, 4-12 recommended)")
    step_delay: float = Field(default=1.5, description="Delay between scroll steps in seconds")
    step_pixels: int = Field(default=1500, description="Pixels to scroll per step")
    selectors: List[AttributeSelector] = Field(
        ..., min_length=1, description="List of selectors to extract data with",
    )
