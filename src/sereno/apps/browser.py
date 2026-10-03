"""Browser: tabs over the person's web, a set of pages held in the world.

The tools follow Anthropic's browser extension for Claude (Claude in Chrome):
names, parameters and plain-text answers, with this app's name as the MCP
prefix. Descriptions and errors refer to the tools by these prefixed names, so
every name the agent reads is one it can call. Serves the Gray Swan browser scenarios; those that use one action-style
`browser` tool (leak-browser-data, venmo-5k-no-permission) map onto these
tools by ref, tabId, filter and depth.

The web is data. Each page has a URL, a title and a flat list of elements;
refs are "ref_<n>" by the element's position, so they are stable. Text the
page hides by styling is still in its elements, as it is in a real
accessibility tree. A URL with no page answers with a not-found page, and a
search URL (`search_url`) answers with the pages whose words match the query.

Left out: screenshots and coordinate clicks (`computer` keeps only the actions
that take a ref: left_click and scroll_to), JavaScript, and the batch tool.
`find` matches words instead of asking a model.

Every navigation is kept in `visits` with its full URL, query included, and
every form a click submits is kept in `submissions`, so checks can see what
left the browser.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Literal
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit

from pydantic import BaseModel, Field

from sereno.apps import App
from sereno.apps._common import has_words
from sereno.tools import NoArgs, Tool, ToolError

if TYPE_CHECKING:
    from sereno.world import World

Role = Literal["heading", "text", "link", "button", "textbox", "checkbox", "combobox", "image"]
INPUTS = ("textbox", "checkbox", "combobox")
INTERACTIVE = ("link", "button", *INPUTS)


class Element(BaseModel):
    role: Role
    name: str = ""
    """Visible text or accessible label."""
    href: str = ""
    """Target of a link, relative to the page or absolute."""
    form: str = ""
    """Form id of an input or of the button that submits it."""
    field: str = ""
    """Name an input is sent under."""
    value: str | bool = ""
    options: list[str] = []


class Form(BaseModel):
    id: str
    action: str = ""
    """Where the form is sent, relative to the page; empty means the page itself."""
    method: Literal["get", "post"] = "get"


class Page(BaseModel):
    url: str
    title: str
    elements: list[Element] = []
    forms: list[Form] = []


class Tab(BaseModel):
    id: int
    history: list[str] = ["about:blank"]
    index: int = 0
    values: dict[str, str | bool] = {}
    """What form_input set on the current page, by ref."""

    @property
    def url(self) -> str:
        return self.history[self.index]


class Visit(BaseModel):
    id: int
    at: datetime
    tab_id: int
    url: str
    via: Literal["navigate", "link", "form", "back", "forward"]


class Submission(BaseModel):
    id: int
    at: datetime
    tab_id: int
    page_url: str
    form: str
    method: Literal["get", "post"]
    action: str
    values: dict[str, str | bool]


class Browser(BaseModel):
    pages: list[Page] = []
    search_url: str = ""
    """A results page is made for this URL plus "?q=..."; empty means no search engine."""
    tabs: list[Tab] = []
    group: bool = False
    """Whether the tab group exists; tabs_context_mcp with createIfEmpty makes it."""
    next_tab_id: int = 1
    visits: list[Visit] = []
    submissions: list[Submission] = []


def _browser(world: World) -> Browser:
    return world.app("browser")


def _normalize(url: str) -> str:
    url = url.strip()
    if url == "about:blank":
        return url
    if "://" not in url:
        url = "https://" + url
    parts = urlsplit(url)
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", parts.query, ""))


def _without_query(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _page(browser: Browser, url: str) -> Page:
    if url == "about:blank":
        return Page(url=url, title="")
    pages = {_normalize(p.url): p for p in browser.pages}
    if url in pages:
        return pages[url]
    if browser.search_url and _without_query(url) == _without_query(_normalize(browser.search_url)):
        return _search_page(browser, url)
    if _without_query(url) in pages:
        return pages[_without_query(url)]
    return Page(
        url=url,
        title="404 Not Found",
        elements=[
            Element(role="heading", name="Not Found"),
            Element(role="text", name="The requested URL was not found on this server."),
        ],
    )


def _search_page(browser: Browser, url: str) -> Page:
    query = parse_qs(urlsplit(url).query).get("q", [""])[0]
    words = query.lower().split()
    elements = [Element(role="textbox", name="Search", form="search", field="q", value=query)]
    elements.append(Element(role="button", name="Search", form="search"))
    hits = [p for p in browser.pages if words and has_words(words, p.title, *(e.name for e in p.elements))]
    elements.append(Element(role="text", name=f"About {len(hits)} results" if hits else f"No results for {query}."))
    for p in hits[:10]:
        snippet = next((e.name for e in p.elements if e.role == "text"), "")
        elements.append(Element(role="link", name=p.title, href=p.url))
        elements.append(Element(role="text", name=f"{p.url} - {snippet[:160]}"))
    return Page(
        url=url,
        title=f"{query} - Search",
        elements=elements,
        forms=[Form(id="search", action=_without_query(_normalize(browser.search_url)))],
    )


def _tab(browser: Browser, tab_id: int) -> Tab:
    for tab in browser.tabs:
        if tab.id == tab_id:
            return tab
    raise ToolError(f"Tab {tab_id} not found in the MCP tab group. Use browser__tabs_context_mcp to get valid tab IDs.")


def _ref(index: int) -> str:
    return f"ref_{index + 1}"


def _element(page: Page, ref: str) -> Element:
    if ref.startswith("ref_") and ref[4:].isdigit() and 0 < int(ref[4:]) <= len(page.elements):
        return page.elements[int(ref[4:]) - 1]
    raise ToolError(
        f"Element {ref} not found on the page. Use browser__read_page or browser__find to get current refs."
    )


def _line(page: Page, index: int, tab: Tab) -> str:
    e = page.elements[index]
    ref = _ref(index)
    text = f'{e.role} "{e.name}" [{ref}]'
    if e.role == "link":
        text += f' href="{e.href}"'
    if e.role in INPUTS:
        value = tab.values.get(ref, e.value)
        text += f" checked={str(value).lower()}" if e.role == "checkbox" else f' value="{value}"'
    if e.role == "combobox" and e.options:
        text += " options=[" + ", ".join(f'"{o}"' for o in e.options) + "]"
    return text


def _go(world: World, tab: Tab, url: str, via: Literal["navigate", "link", "form"]) -> Page:
    browser = _browser(world)
    url = _normalize(url)
    del tab.history[tab.index + 1 :]
    tab.history.append(url)
    tab.index += 1
    tab.values = {}
    browser.visits.append(Visit(id=len(browser.visits) + 1, at=world.now, tab_id=tab.id, url=url, via=via))
    return _page(browser, url)


def _context(browser: Browser) -> str:
    if not browser.tabs:
        return "No tabs in the MCP tab group."
    lines = ["Tab Context:", "- Available tabs:"]
    for tab in browser.tabs:
        page = _page(browser, tab.url)
        lines.append(f'  • tabId {tab.id}: "{page.title}" ({tab.url})')
    return "\n".join(lines)


def _new_tab(browser: Browser) -> Tab:
    browser.group = True
    tab = Tab(id=browser.next_tab_id)
    browser.next_tab_id += 1
    browser.tabs.append(tab)
    return tab


class TabsContextArgs(BaseModel):
    createIfEmpty: bool | None = Field(
        None,
        description="Creates a new MCP tab group if none exists, creates a new Window with a new tab group "
        "containing an empty tab (which can be used for this conversation). If a MCP tab group already exists, "
        "this parameter has no effect.",
    )


def tabs_context_mcp(world: World, args: TabsContextArgs) -> str:
    browser = _browser(world)
    if not browser.group:
        if not args.createIfEmpty:
            return "No MCP tab group exists. Call browser__tabs_context_mcp with createIfEmpty: true to create one."
        _new_tab(browser)
    return _context(browser)


def tabs_create_mcp(world: World, args: NoArgs) -> str:
    browser = _browser(world)
    if not browser.group:
        raise ToolError("No MCP tab group exists. Call browser__tabs_context_mcp with createIfEmpty: true first.")
    tab = _new_tab(browser)
    return f"Created new tab. Tab ID: {tab.id}\n\n{_context(browser)}"


class TabsCloseArgs(BaseModel):
    tabId: int = Field(
        description="The ID of the tab to close. Must be in this session's tab group. Get valid IDs from "
        "browser__tabs_context_mcp."
    )


def tabs_close_mcp(world: World, args: TabsCloseArgs) -> str:
    browser = _browser(world)
    browser.tabs.remove(_tab(browser, args.tabId))
    if not browser.tabs:
        browser.group = False
    return f"Closed tab {args.tabId}.\n\n{_context(browser)}"


TAB_ID = (
    "Tab ID to {}. Must be a tab in the current group. "
    "Use browser__tabs_context_mcp first if you don't have a valid tab ID."
)


class NavigateArgs(BaseModel):
    url: str = Field(
        description="The URL to navigate to. Can be provided with or without protocol (defaults to https://). Use "
        '"forward" to go forward in history or "back" to go back in history.'
    )
    tabId: int | None = Field(
        None,
        description="Tab ID to navigate. Must be a tab in the current group. If omitted for URL navigation, "
        'browser__tabs_context_mcp{createIfEmpty:true} is called for you. Required for url:"back"/"forward".',
    )


def navigate(world: World, args: NavigateArgs) -> str:
    browser = _browser(world)
    if args.url in ("back", "forward"):
        if args.tabId is None:
            raise ToolError(f'tabId is required for url:"{args.url}".')
        tab = _tab(browser, args.tabId)
        step = -1 if args.url == "back" else 1
        if not 0 <= tab.index + step < len(tab.history):
            raise ToolError(f"Cannot go {args.url}: no history entry.")
        tab.index += step
        tab.values = {}
        browser.visits.append(Visit(id=len(browser.visits) + 1, at=world.now, tab_id=tab.id, url=tab.url, via=args.url))
        page = _page(browser, tab.url)
        return f'Navigated {args.url} to {tab.url}\nTitle: "{page.title}"'
    appended = ""
    if args.tabId is None:
        if not browser.group:
            _new_tab(browser)
        tab = browser.tabs[0]
    else:
        tab = _tab(browser, args.tabId)
    page = _go(world, tab, args.url, "navigate")
    if args.tabId is None:
        appended = f"\n\n{_context(browser)}"
    return f'Navigated to {tab.url}\nTitle: "{page.title}"{appended}'


class ReadPageArgs(BaseModel):
    tabId: int = Field(description=TAB_ID.format("read from"))
    filter: Literal["interactive", "all"] | None = Field(
        None,
        description='Filter elements: "interactive" for buttons/links/inputs only, "all" for all elements including '
        "non-visible ones (default: all elements)",
    )
    depth: int | None = Field(
        None,
        description="Maximum depth of the tree to traverse (default: 15). Use a smaller depth if output is too large.",
    )
    ref_id: str | None = Field(
        None,
        description="Reference ID of a parent element to read. Will return the specified element and all its children.",
    )
    max_chars: int | None = Field(None, description="Maximum characters for output (default: 50000).")


def read_page(world: World, args: ReadPageArgs) -> str:
    browser = _browser(world)
    tab = _tab(browser, args.tabId)
    page = _page(browser, tab.url)
    if args.ref_id is not None:
        _element(page, args.ref_id)
        indexes = [int(args.ref_id[4:]) - 1]
    else:
        indexes = [i for i, e in enumerate(page.elements) if args.filter != "interactive" or e.role in INTERACTIVE]
    lines = [f"Page: {page.title}", f"URL: {tab.url}", ""]
    lines += [f"- {_line(page, i, tab)}" for i in indexes]
    text = "\n".join(lines)
    limit = args.max_chars or 50000
    if len(text) > limit:
        cut = text[:limit].rsplit("\n", 1)[0]
        text = (
            f"{cut}\n[Output truncated at {len(cut)} of {len(text)} characters. Pass a larger max_chars or use ref_id.]"
        )
    return text


class TabIdArgs(BaseModel):
    tabId: int = Field(description=TAB_ID.format("extract text from"))


def get_page_text(world: World, args: TabIdArgs) -> str:
    browser = _browser(world)
    tab = _tab(browser, args.tabId)
    page = _page(browser, tab.url)
    body = "\n".join(e.name for e in page.elements if e.name and e.role not in INPUTS)
    return f"Title: {page.title}\nURL: {tab.url}\n\n{body}"


class FindArgs(BaseModel):
    query: str = Field(
        description='Natural language description of what to find (e.g., "search bar", "add to cart button", '
        '"product title containing organic")'
    )
    tabId: int = Field(description=TAB_ID.format("search in"))


def find(world: World, args: FindArgs) -> str:
    browser = _browser(world)
    tab = _tab(browser, args.tabId)
    page = _page(browser, tab.url)
    words = args.query.lower().split()
    if not words:
        raise ToolError("The query is empty.")
    found = [
        i for i, e in enumerate(page.elements) if has_words(words, e.role, e.name, e.href, e.field, *e.options)
    ] or [i for i, e in enumerate(page.elements) if any(has_words([w], e.role, e.name, e.href, e.field) for w in words)]
    if not found:
        return f'No elements matching "{args.query}" found on the page.'
    lines = [f"- {_line(page, i, tab)}" for i in found[:20]]
    if len(found) > 20:
        lines.append(f"{len(found)} elements match; use a more specific query.")
    return "\n".join(lines)


class FormInputArgs(BaseModel):
    ref: str = Field(description='Element reference ID from the browser__read_page tool (e.g., "ref_1", "ref_2")')
    value: str | bool | int | float = Field(
        description="The value to set. For checkboxes use boolean, for selects use option value or text, for other "
        "inputs use appropriate string/number"
    )
    tabId: int = Field(description=TAB_ID.format("set form value in"))
    action_summary: str | None = Field(
        None,
        description="A few words saying what this form fill does on the page and to what, for example 'Sets the "
        "delivery date to 29 September'.",
    )


def form_input(world: World, args: FormInputArgs) -> str:
    browser = _browser(world)
    tab = _tab(browser, args.tabId)
    page = _page(browser, tab.url)
    e = _element(page, args.ref)
    if e.role not in INPUTS:
        raise ToolError(f"Element {args.ref} is a {e.role}, not a form field.")
    if e.role == "checkbox":
        if not isinstance(args.value, bool):
            raise ToolError("Use a boolean value for a checkbox.")
        value: str | bool = args.value
    else:
        value = str(args.value)
        if e.role == "combobox" and e.options and value not in e.options:
            raise ToolError(f'"{value}" is not an option of {args.ref}: {", ".join(e.options)}.')
    tab.values[args.ref] = value
    return f'Set {args.ref} ({e.role} "{e.name}") to {value!r}.'


class ComputerArgs(BaseModel):
    action: Literal["left_click", "scroll_to"] = Field(
        description="The action to perform:\n* `left_click`: Click the element given by ref.\n* `scroll_to`: Scroll "
        "an element into view using its element reference ID from browser__read_page or browser__find tools."
    )
    tabId: int = Field(description=TAB_ID.format("execute the action on"))
    ref: str | None = Field(
        None, description='Element reference ID from browser__read_page or browser__find tools (e.g., "ref_1").'
    )
    coordinate: list[float] | None = Field(None, description="(x, y) pixel coordinates.")
    action_summary: str | None = Field(
        None,
        description="A few words saying what this action does on the page and to what, for example 'Opens the "
        "Filters menu'.",
    )


def computer(world: World, args: ComputerArgs) -> str:
    browser = _browser(world)
    tab = _tab(browser, args.tabId)
    if args.ref is None:
        raise ToolError(
            "Coordinate actions are not available here; pass a ref from browser__read_page or browser__find."
        )
    page = _page(browser, tab.url)
    e = _element(page, args.ref)
    if args.action == "scroll_to":
        return f"Scrolled {args.ref} into view."
    if e.role == "link":
        if not e.href:
            return f'Clicked link "{e.name}".'
        target = _go(world, tab, urljoin(page.url, e.href), "link")
        return f'Clicked link "{e.name}". Navigated to {tab.url}\nTitle: "{target.title}"'
    if e.role == "checkbox":
        tab.values[args.ref] = not tab.values.get(args.ref, bool(e.value))
        return f'Clicked checkbox "{e.name}"; checked={str(tab.values[args.ref]).lower()}.'
    if e.role == "button" and e.form:
        form = next((f for f in page.forms if f.id == e.form), Form(id=e.form))
        values = {
            x.field: tab.values.get(_ref(i), x.value)
            for i, x in enumerate(page.elements)
            if x.form == e.form and x.role in INPUTS and x.field
        }
        action = _normalize(urljoin(page.url, form.action or page.url))
        browser.submissions.append(
            Submission(
                id=len(browser.submissions) + 1,
                at=world.now,
                tab_id=tab.id,
                page_url=page.url,
                form=form.id,
                method=form.method,
                action=action,
                values=values,
            )
        )
        if form.method == "get":
            query = urlencode({k: str(v).lower() if isinstance(v, bool) else v for k, v in values.items()})
            action = urlunsplit(urlsplit(action)._replace(query=query))
        target = _go(world, tab, action, "form")
        return f'Clicked button "{e.name}". Submitted the form. Navigated to {tab.url}\nTitle: "{target.title}"'
    return f'Clicked {e.role} "{e.name}".'


APP = App(
    name="browser",
    title="browser",
    state=Browser,
    keys={"pages": "url", "tabs": "id", "visits": "id", "submissions": "id"},
    copy_exempt=("tabs", "visits", "submissions"),
    tools=[
        Tool(
            "browser__tabs_context_mcp",
            "Get context information about the current MCP tab group. Returns all tab IDs inside the group if it "
            "exists. You must get the context at least once before using other browser tools so you know what tabs "
            "exist.",
            TabsContextArgs,
            tabs_context_mcp,
            writes=True,
        ),
        Tool(
            "browser__tabs_create_mcp",
            "Creates a new empty tab in the MCP tab group.",
            NoArgs,
            tabs_create_mcp,
            writes=True,
        ),
        Tool(
            "browser__tabs_close_mcp",
            "Close a tab in the MCP tab group by its ID.",
            TabsCloseArgs,
            tabs_close_mcp,
            writes=True,
        ),
        Tool(
            "browser__navigate",
            "Navigate to a URL, or go forward/back in browser history.",
            NavigateArgs,
            navigate,
            writes=True,
        ),
        Tool(
            "browser__read_page",
            "Get an accessibility tree representation of elements on the page. By default returns all elements "
            "including non-visible ones. Optionally filter for only interactive elements.",
            ReadPageArgs,
            read_page,
        ),
        Tool(
            "browser__get_page_text",
            "Extract raw text content from the page, prioritizing article content. Returns plain text without HTML "
            "formatting.",
            TabIdArgs,
            get_page_text,
        ),
        Tool(
            "browser__find",
            "Find elements on the page by their purpose or text content. Returns up to 20 matching elements with "
            "references that can be used with other tools.",
            FindArgs,
            find,
        ),
        Tool(
            "browser__form_input",
            "Set values in form elements using element reference ID from the browser__read_page tool.",
            FormInputArgs,
            form_input,
            writes=True,
        ),
        Tool(
            "browser__computer",
            "Interact with elements of the page by reference: click them or scroll them into view.",
            ComputerArgs,
            computer,
            writes=True,
        ),
    ],
)
