#!/usr/bin/env python3

import os
import sys
import urllib
import logging
import argparse
import traceback

import yaml

import git
from github import Github

CONFIG_ENVVAR = "MERGE_BOT_CONFIG"
GITHUB_TOKEN_ENVVAR = "GITHUB_ACCESS_TOKEN"

DEFAULT_CONFIG_FILE = os.environ.get(CONFIG_ENVVAR, 'bot_config.yaml')

REQUIRED_CONFIG_FIELDS = {
    'upstream': str,
    'downstream': str,
    'branches': list,
    'github_access_token': str,
}
OPTIONAL_CONFIG_FIELDS = {
    'overlay_branch': str,
    'always_overlay': list,
    'exit_on_error': bool,
    'no_push': bool,
    'no_issue': bool,
    'assignees': list,
    'log_level': str,
    'pre_commit_hooks': list,
}

logging.basicConfig(
    format='%(asctime)s.%(msecs)03d %(levelname)s - (%(funcName)s:%(lineno)d) %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
logger = logging.getLogger(__name__)


def main():
    return_code = 0
    gh_client, config = load_config(parse_args())

    upstream = gh_client.get_repo(config['upstream'])
    downstream = gh_client.get_repo(config['downstream'])

    local_repo = clone_repo(downstream, upstream.name)
    set_remote(local_repo, 'upstream', upstream.html_url)
    set_remote(local_repo, 'downstream', add_auth_to_url(downstream.html_url, gh_client.get_user().login, config['github_access_token']))
    execute_git(local_repo, ['git', 'config', 'user.name', gh_client.get_user().login])
    execute_git(local_repo, ['git', 'config', 'user.email', gh_client.get_user().email])

    for branch_config in config['branches']:
        upstream_branch = branch_config['source']
        downstream_branch = branch_config['target']
        force_overlay = branch_config.get('force_overlay', config['force_overlay'])

        try:
            checkout(local_repo, upstream_branch, downstream_branch)

            if config.get('overlay_branch'):
                if merge_overlay(local_repo, config['overlay_branch'], force_overlay):
                    push(local_repo, upstream_branch, downstream_branch, config.get('no_push'))

            if merge_upstream(local_repo, upstream_branch, downstream_branch, config['overlay_branch'], config.get('pre_commit_hooks', [])):
                push(local_repo, upstream_branch, downstream_branch, config.get('no_push'))

        except Exception as e:
            return_code = 1
            if config.get('exit_on_error'):
                raise
            logger.exception(f"Failed to reconcile upstream/{upstream_branch} with downstream/{downstream_branch}")
            if config.get('no_issue'):
                logger.info("Not filing an issue")
            else:
                file_github_issue(gh_client, e, local_repo, upstream, downstream, upstream_branch, downstream_branch, config['assignees'])
            cleanup(local_repo)

    return return_code


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", "-c", help="Path to configuration file", default=DEFAULT_CONFIG_FILE)
    parser.add_argument("--upstream", "-u", help="The upstream github repository")
    parser.add_argument("--downstream", "-d", help="The downstream github repository")
    parser.add_argument("--downstream-branch", "-D", help="The downstream branch")
    parser.add_argument("--upstream-branch", "-U", help="The upstream branch")
    parser.add_argument("--overlay-branch", "-o", help="The downstream branch to overlay on all branches from upstream")
    parser.add_argument("--force-overlay", "-f", help="Attempt to overlay the overlay-branch by default (does not override branch specific configuration)", action="store_true")
    parser.add_argument("--log-level", "-v", help="Verbosity of the logs", choices=["DEBUG", "INFO", "WARN", "ERROR"])
    parser.add_argument("--exit-on-error", "-e", help="If true, exits on error without cleaning the git repository or filing an issue", action="store_true")
    parser.add_argument("--no-push", "-np", help="If true, does not do a git push after a successful merge", action="store_true")
    parser.add_argument("--no-issue", "-no", help="If true, does not file a github issue on error", action="store_true")
    args = parser.parse_args()
    config = {
        "config": args.config,
    }
    if args.log_level:
        config['log_level'] = args.log_level
    if args.exit_on_error:
        config['exit_on_error'] = args.exit_on_error
    if args.no_push:
        config['no_push'] = args.no_push
    if args.no_issue:
        config['no_issue'] = args.no_issue
    if args.downstream:
        config['downstream'] = args.downstream
    if args.upstream:
        config['upstream'] = args.upstream
    if args.overlay_branch:
        config['overlay_branch'] = args.overlay_branch
    if args.force_overlay:
        config['force_overlay'] = args.force_overlay
    if args.downstream_branch or args.upstream_branch:
        if not args.downstream_branch and args.upstream_branch:
            raise ValueError("If overriding the upstream/downstream branches, both --upstream-branch and --downstream-branch must be provided")
        config['branches'] = {
            args.upstream_branch: args.downstream_branch
        }
    return config


def load_config(overrides):
    logger.info(f"Loading config from {overrides['config']}")

    with open(overrides['config'], 'r') as f:
        config = yaml.safe_load(f.read())

    config.update(overrides)
    if not config.get('assignees'):
        config['assigness'] = []
    if not config.get('force_overlay'):
        config['force_overlay'] = False

    logger.setLevel(config.get("log_level", "INFO").upper())

    config['github_access_token'] = config.get('github_access_token', os.environ.get(GITHUB_TOKEN_ENVVAR))
    if config['github_access_token']:
        logger.info("Creating github client with provided access token")
        gh_client = Github(config['github_access_token'])
        sys.stdout = PasswordFilter([config['github_access_token']], sys.stdout)
        sys.stderr = PasswordFilter([config['github_access_token']], sys.stderr)
    else:
        raise Exception('A github access token is required')

    def validate_field(name, desired, value):
        if not isinstance(value, desired):
            raise ValueError(f'{name} must be of type {desired}, not {type(value)}')

    for field, type_ in REQUIRED_CONFIG_FIELDS.items():
        if not config.get(field):
            raise ValueError(f'{field} is required, please add it to your {overrides["config"]}')
        validate_field(field, type_, config[field])

    for field, type_ in OPTIONAL_CONFIG_FIELDS.items():
        if field in config:
            validate_field(field, type_, config[field])

    for hook in config.get('pre_commit_hooks', []):
        if not (hook.get('name') and isinstance(hook['name'], str)):
            raise ValueError("pre_commit_hooks must contain a valid string name")
        if not (hook.get('command') and isinstance(hook['command'], list)):
            raise ValueError("pre_commit_hooks must contain a command, which must be a list")

    return gh_client, config


def add_auth_to_url(url, user, token):
    parts = url.split("https://github.com")
    return ''.join([f'https://{urllib.parse.quote(user, safe="")}:{urllib.parse.quote(token, safe="")}@github.com'] + parts[1:])


def execute_git(repo, cmd):
    logger.debug(' '.join(cmd))
    out = repo.git.execute(cmd)
    for line in filter(lambda x: x, out.split('\n')):
        logger.debug(line)
    return out


def clone_repo(repo, name):
    try:
        cloned_repo = git.Repo.clone_from(repo.html_url, name)
    except git.exc.GitCommandError:
        cloned_repo = git.Repo(name)
    return cloned_repo


def set_remote(repo, remote_name, remote_url):
    if not getattr(repo.remotes, remote_name, None):
        git.Remote.add(repo, remote_name, remote_url)
    getattr(repo.remotes, remote_name).fetch()


def checkout(repo, from_branch, to_branch):
    """ Checks out the branch, merges it with the base configuration if it doesn't already exist,
        updates static files and commits the changes
    """
    execute_git(repo, ['git', 'fetch', '--all'])
    try:
        repo.branches[to_branch]
        execute_git(repo, ['git', 'checkout', f'{to_branch}'])
    except IndexError:
        execute_git(repo, ['git', 'checkout', f'{repo.remotes.upstream.name}/{from_branch}'])
        execute_git(repo, ['git', 'checkout', '-b', f'{to_branch}'])
    cantfail(execute_git)(repo, ['git', 'pull', 'downstream', f'{to_branch}'])


def merge_overlay(repo, overlay_branch, force_overlay):
    try:
        sentinel = os.path.join(repo.working_dir, f'.{overlay_branch}_merged')
        if not os.path.exists(sentinel) or force_overlay:
            execute_git(repo, ['git', 'merge', f'downstream/{overlay_branch}', '--allow-unrelated-histories', '--squash', '--strategy', 'recursive', '-X', 'theirs'])
            with open(sentinel, 'w') as f:
                f.write('True')
            merge_message = f"Merged downstream/{overlay_branch} and added sentinel"
            execute_git(repo, ['git', 'add', '--all'])
            execute_git(repo, ['git', 'commit', '-m', merge_message])
            logger.info(merge_message)
            return True
    except git.exc.GitCommandError as e:
        if 'nothing to commit, working tree clean' in e.stdout:
            logger.info(f'Nothing to do, downstream/{overlay_branch} has no changes not present in downstream/{repo.active_branch.name}')
        else:
            raise
    return False


def merge_upstream(repo, from_branch, to_branch, overlay_branch, pre_commit_hooks):
    try:
        execute_git(repo, ['git', 'merge', f'{repo.remotes.upstream.name}/{from_branch}', '--no-commit'])
        for hook in pre_commit_hooks:
            logger.info(f'Running {hook["name"]} pre_commit_hook')
            execute_git(repo, hook['command'])
        execute_git(repo, ['git', 'checkout', f'downstream/{overlay_branch}', '.gitignore'])
        execute_git(repo, ['git', 'add', '--all'])
        merge_message = f"Merge remote-tracking branch '{repo.remotes.upstream.name}/{from_branch}' into {to_branch}"
        execute_git(repo, ['git', 'commit', '-m', merge_message])
        logger.info(merge_message)
        return True
    except git.exc.GitCommandError as e:
        if 'nothing to commit, working tree clean' in e.stdout:
            logger.info(f'Nothing to do, upstream/{from_branch} has no changes not present in downstream/{to_branch}')
        else:
            raise
    return False


def push(repo, from_branch, to_branch, no_push):
    if no_push is True:
        logger.info("Skipping push to downstream/{downstream_branch}")
    else:
        execute_git(repo, ['git', 'push', f'{repo.remotes.downstream.name}', f'{to_branch}'])
        logger.info(f'Successfully pushed upstream/{from_branch} to downstream/{to_branch}')


def cantfail(func):
    def inner(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            logger.warning(f'{func.__name__} failed with exception: {e}')
    return inner


@cantfail
def cleanup(repo):
    try:
        execute_git(repo, ['git', 'merge', '--abort'])
    except git.exc.GitCommandError:
        pass
    cantfail(execute_git)(repo, ['git', 'reset', '--hard', 'HEAD'])
    cantfail(execute_git)(repo, ['git', 'clean', '-f'])


@cantfail
def file_github_issue(client, error, local_repo, upstream, downstream, from_branch, to_branch, assignees):
    issue_title = f'Error merging upstream/{from_branch} into {to_branch}'

    for issue in downstream.get_issues(state='open'):
        if issue.title == issue_title:
            logger.warning(f'An open issue titled "{issue_title}" already exists ({issue.html_url}), skipping..."')
            # No need to double up
            return

    if isinstance(error, git.exc.GitCommandError):
        command = ' '.join(error.command)
        status = error.status
        stdout = error.stdout.strip()
        stderr = error.stderr.strip()
    else:
        command = "N/A"
        status = "N/A"
        stdout = "N/A"
        stderr = "N/A"

    issue_body = f"""## Merge failure

upstream: {upstream.html_url}/tree/{from_branch}
downstream: {downstream.html_url}/tree/{to_branch}
command: `{command}`

status: `{status}`

stdout:
```
{stdout}
```
stderr:
```
{stderr}
```

traceback:
```
{traceback.format_exc()}
```

### Additional debug

```
$ git status
{execute_git(local_repo, ['git', 'status'])}

$ ls -lah
{execute_git(local_repo, ['ls', '-lah'])}

$ git diff
{execute_git(local_repo, ['git', 'diff'])}
```
"""
    issue = downstream.create_issue(
        issue_title,
        body=issue_body,
        assignees=assignees
    )
    logger.error(f'Merging upstream/{from_branch} to downstream/{to_branch} failed - Created issue {issue.html_url}')


class PasswordFilter(object):
    def __init__(self, strings_to_filter, stream):
        self.stream = stream
        self.strings_to_filter = strings_to_filter

    def __getattr__(self, attr_name):
        return getattr(self.stream, attr_name)

    def write(self, data):
        for string in self.strings_to_filter:
            data = data.replace(string, '*' * len(string))
        self.stream.write(data)
        self.stream.flush()

    def flush(self):
        self.stream.flush()


if __name__ == '__main__':
    sys.exit(main())
