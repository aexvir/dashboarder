import operator
import os
import re
import signal
import time
from collections import OrderedDict, namedtuple
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from functools import reduce

import gitlab
import requests
from requests.adapters import HTTPAdapter
from requests.exceptions import ConnectionError
from tornado.ioloop import IOLoop
from tornado.web import Application, HTTPError, RequestHandler, StaticFileHandler
from urllib3.exceptions import ReadTimeoutError
from urllib3.util import Retry

gitlab_host = os.environ.get("GITLAB_HOST")
gitlab_token = os.environ.get("GITLAB_TOKEN")
token = os.environ.get("ACCESS_TOKEN")

session = requests.Session()
session.headers = {"user-agent": "dashboarder/1.0"}
max_retries = Retry(
    total=5, read=5, connect=5, backoff_factor=0.1, status_forcelist=(500, 502, 504)
)
adapter = HTTPAdapter(max_retries=max_retries, pool_maxsize=25)
session.mount("http://", adapter)
session.mount("https://", adapter)

glclient = gitlab.Gitlab(gitlab_host, private_token=gitlab_token, session=session)

in_memory_data = {"groups": {}, "projects": {}, "boards": {}}


def fetch_collection(manager, extra_filters=None):
    filters = {"as_list": False}

    if extra_filters:
        filters.update(extra_filters)

    rest_object_list = manager.list(**filters)
    result = []

    start = time.time()
    with ThreadPoolExecutor(max_workers=25) as executor:

        def gather_response(page):
            api_response = manager.list(**filters, page=page)
            result.extend(api_response)

        wait(
            [
                executor.submit(gather_response, index)
                for index in range(1, rest_object_list.total_pages + 1)
            ]
        )
    end = time.time()

    print(
        f"\tfetched {len(result)} objects using {type(manager).__name__} in {end - start}s"
    )

    return set(result)


def fetch_groups():
    print("Fetching GitLab groups...")
    in_memory_data["groups"] = {
        group.id: group for group in fetch_collection(glclient.groups)
    }


def fetch_projects():
    print("Fetching GitLab projects...")
    in_memory_data["projects"] = {
        project.id: project
        for project in fetch_collection(glclient.projects, {"archived": False})
    }


def get_object(manager, object_id, key, extra_filters=None):
    filters = {"id": object_id, "archived": False}

    if extra_filters:
        filters.update(extra_filters)

    if object_id not in in_memory_data[key]:
        try:
            obj = manager.get(**filters)
        except gitlab.GitlabGetError:
            raise HTTPError(404)

        in_memory_data[key][object_id] = obj

    return in_memory_data[key][object_id]


def has_secret(code):
    def decorated(self, *args, **kwargs):
        secret = self.get_cookie("secret")

        if secret and secret == token:
            return code(self, *args, **kwargs)
        else:
            raise HTTPError(403)

    return decorated


# Gitlab doesn't offer a way for getting label colors yet...
class LabelProvider:
    def __init__(self):
        self.known_labels = {
            "difficulty:high": "#ca0c00",
            "difficulty:low": "#69d100",
            "difficulty:medium": "#f0ad4e",
            "difficulty:newcomer": "#a8d695",
            "importance:critical": "#ff0000",
            "importance:high": "#cc0033",
            "importance:low": "#69d100",
            "importance:medium": "#f0ad4e",
            "next milestone": "#0033cc",
            "status:blocked": "#ca0400",
            "status:invalid": "#c8c6ca",
            "status:needhelp": "#5cb85c",
            "status:needinfo": "#ca0200",
            "status:review": "#428bca",
            "status:today": "#d10069",
            "status:wip": "#7f8c8d",
            "status:wontfix": "#34495e",
            "this milestone": "#004e00",
            "type:bug": "#00cac8",
            "type:feature": "#00c8ca",
            "type:question": "#00c4ca",
            "type:refactor": "#00caca",
        }

    def get_text_color(self, hex_color):
        hex_color = hex_color.replace("#", "")
        (r, g, b) = (hex_color[:2], hex_color[2:4], hex_color[4:])
        val = (
            0
            if 1 - (int(r, 16) * 0.299 + int(g, 16) * 0.587 + int(b, 16) * 0.114) / 255
            < 0.35
            else 255
        )
        return f"rgba({val}, {val}, {val}, .9)"

    def get_style(self, label):
        background_color = self.known_labels.get(label, "#01c8ca")
        return {
            "background": background_color,
            "text": self.get_text_color(background_color),
        }


class BaseHandler(RequestHandler):
    def __init__(self, application, request, **kwargs):
        super(BaseHandler, self).__init__(application, request)
        self.in_memory_data = kwargs.get("data")

    def write_error(self, status_code, **kwargs):
        self.set_status(status_code)
        self.render(f"templates/status/http{status_code}.html", page=None)


class DefaultHandler(BaseHandler):
    def get(self):
        raise HTTPError(404)


class GroupListHandler(BaseHandler):
    @has_secret
    def get(self):
        print("GroupListHandler.get")

        if not self.in_memory_data["groups"]:
            fetch_groups()

        self.render(
            "templates/group_list.html", groups=self.in_memory_data["groups"].values()
        )


class GroupDetailsHandler(BaseHandler):
    @has_secret
    def get(self, group_id):
        print(f"GroupDetailsHandler.get group_id={group_id}")
        group_id = int(group_id)

        group = get_object(glclient.groups, group_id, "groups")

        self.render(
            "templates/group_details.html",
            boards=fetch_collection(group.boards),
            group=group,
            projects=fetch_collection(group.projects),
            subgroups=fetch_collection(group.subgroups),
        )


class ProjectBoardsListHandler(BaseHandler):
    @has_secret
    def get(self, project_id):
        print(f"GroupDetailsHandler.get project_id={project_id}")
        project_id = int(project_id)

        project = get_object(glclient.projects, project_id, "projects")
        group = get_object(
            glclient.groups, project.attributes["namespace"]["id"], "groups"
        )
        boards = fetch_collection(project.boards)

        self.render(
            "templates/project_details.html",
            boards=boards,
            group=group,
            project=project,
        )


@dataclass
class Column:
    title: str = "Placeholder"
    classname: str = "placeholer"
    position: int = 0
    color: str = "#428bca"
    issues: set = field(default_factory=set)


class Dashboard:
    def __init__(self, issues=[]):
        self.columns = {
            "backlog": Column("Backlog", "backlog", 0, "#767676", issues),
            "closed": Column("Closed", "closed", 1, "#21ba45"),
        }

        self.filter_issues("closed", (IssueCondition("state", operator.eq, "closed"),))

    @property
    def colors(self):
        return [(column.classname, column.color) for _, column in self.columns.items()]

    def add_column(self, title, classname, color):
        num_columns = len(self.columns)
        self.columns[classname] = Column(title, classname, num_columns - 1, color)
        self.columns["closed"].position = num_columns

    def filter_issues(self, column_id, criteria):
        backlog = self.columns["backlog"]
        self.columns[column_id].issues = {
            issue for issue in backlog.issues if issue_filter(issue, criteria)
        }
        backlog.issues -= self.columns[column_id].issues


IssueCondition = namedtuple("IssueCondition", ["key", "operator", "value"])
ColumnDefinition = namedtuple(
    "ColumnDefinition", ["title", "class_name", "color", "conditions"]
)


def issue_filter(issue, conditions):
    return all(
        [
            condition.operator(
                reduce(
                    lambda d, key: d.get(key) if isinstance(d, dict) else None,
                    condition.key.split("."),
                    issue.attributes,
                ),
                condition.value,
            )
            for condition in conditions
        ]
    )


def _generate_column(board_list):
    if board_list.attributes["label"]:
        board_list_name = board_list.attributes["label"]["name"]
        class_name = re.sub(r"\W+", "", board_list_name)
        color_hex = board_list.attributes["label"]["color"]

        conditions = (
            IssueCondition("state", operator.eq, "opened"),
            IssueCondition("labels", operator.contains, board_list_name),
        )

    elif board_list.attributes["assignee"]:
        board_list_assignee = board_list.attributes["assignee"]
        board_list_name = board_list_assignee["name"]
        class_name = "assignee"
        color_hex = None

        conditions = (
            IssueCondition("state", operator.eq, "opened"),
            IssueCondition("assignee.id", operator.eq, board_list_assignee["id"]),
        )

    else:
        # TODO: Imprement milestone lists
        board_list_name = "NotImplemented"
        class_name = "notimplemented"
        color_hex = None
        conditions = []

    return ColumnDefinition(board_list_name, class_name, color_hex, conditions)


def _generate_dashboard(board, issues):
    dashboard = Dashboard(issues)

    for board_list in board.lists.list(all=True):
        column_definition = _generate_column(board_list)
        dashboard.add_column(
            column_definition.title,
            column_definition.class_name,
            column_definition.color,
        )
        dashboard.filter_issues(
            column_definition.class_name, column_definition.conditions
        )

    return dashboard


class ProjectBoardHandler(BaseHandler):
    @has_secret
    def get(self, project_id, board_id):
        print(f"ProjectBoardHandler.get project_id={project_id} board_id={board_id}")
        project_id, board_id = int(project_id), int(board_id)

        project = get_object(glclient.projects, project_id, "projects")
        group = get_object(
            glclient.groups, project.attributes["namespace"]["id"], "groups"
        )
        board = get_object(project.boards, board_id, "boards")

        issues = {
            issue
            for issue in fetch_collection(
                project.issues,
                {"milestone": board.milestone["title"] if board.milestone else None},
            )
        }

        dashboard = _generate_dashboard(board, issues)

        self.render(
            "templates/board.html",
            board=dashboard,
            group=group,
            projects=self.in_memory_data["projects"],
            milestone=board.milestone,
            total=sum([len(column.issues) for _, column in dashboard.columns.items()]),
            label_provider=LabelProvider(),
        )


class GroupBoardHandler(BaseHandler):
    @has_secret
    def get(self, group_id, board_id):
        print(f"GroupBoardHandler.get group_id={group_id} board_id={board_id}")
        group_id, board_id = int(group_id), int(board_id)

        group = get_object(glclient.groups, group_id, "groups")
        board = get_object(group.boards, board_id, "boards")

        issues = {
            issue
            for issue in fetch_collection(
                group.issues,
                {"milestone": board.milestone["title"] if board.milestone else None},
            )
            if issue.project_id in self.in_memory_data["projects"]
        }

        dashboard = _generate_dashboard(board, issues)

        self.render(
            "templates/board.html",
            board=dashboard,
            group=group,
            projects=self.in_memory_data["projects"],
            milestone=board.milestone,
            total=sum([len(column.issues) for _, column in dashboard.columns.items()]),
            label_provider=LabelProvider(),
        )


def make_app():
    print("Dashboarder ready to take requests!")
    return Application(
        [
            (r"/", GroupListHandler, {"data": in_memory_data}),
            (r"/groups", GroupListHandler, {"data": in_memory_data}),
            (r"/groups/([^/]+)", GroupDetailsHandler, {"data": in_memory_data}),
            (
                r"/groups/([^/]+)/boards/([^/]+)",
                GroupBoardHandler,
                {"data": in_memory_data},
            ),
            (r"/projects/([^/]+)", ProjectBoardsListHandler, {"data": in_memory_data}),
            (
                r"/projects/([^/]+)/boards/([^/]+)",
                ProjectBoardHandler,
                {"data": in_memory_data},
            ),
            (r"/static/(.*)", StaticFileHandler, {"path": f"dist"}),
        ],
        default_handler_class=DefaultHandler,
    )


if __name__ == "__main__":
    app = make_app()
    app.listen(5000)

    print("Caching GitLab objects...")
    IOLoop.current().add_callback(fetch_groups)
    IOLoop.current().add_callback(fetch_projects)

    try:
        IOLoop.current().start()

    except KeyboardInterrupt:
        print("\nStopping IOLoop...")
        IOLoop.current().stop()
