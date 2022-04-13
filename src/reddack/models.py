from __future__ import annotations
import abc
from typing import (
    Type,
    Union,
    TypedDict
)
from pathlib import Path

# 3rd party imports
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from praw import Reddit
from praw.models import Submission
import jsonpickle

# Local imports
from reddack.payload import (
    build_removal_block,
    build_submission_block, 
    build_response_block, 
    build_archive_blocks
)
from reddack.exceptions import (
    MsgSendError
)
from reddack.slack import (
    ReddackResponse,
    SubmissionResponse
)
from reddack.utils import (
    get_known_items,
    find_post_requests,
    update_knownitems_file,
    cleanup_knownitems_json,
    cleanup_postrequest_json
)

# TODO Add functionality for flairing posts
# TODO Add functionality for awarding posts

class ReddackItem:
    """Stores information about the state of an item in the modqueue."""
    def __init__(self, prawitem):
        self.prawitem = prawitem.id
        self.message_ts = None
        self.responses: dict[str, ReddackResponse] = {}

    def process_slack_responses(self, post_dir):
        """Check for responses to mod item message."""
        requests, timestamps = find_post_requests(self, post_dir)
        if requests:
            for request, timestamp in zip(requests, timestamps):
                moderator = request['user']['id']
                if moderator in self.responses:
                    self.responses[moderator].update(request, timestamp)
                else:
                    self.initialize_response(moderator)
                    self.responses[moderator].update(request, timestamp)

class ReddackComment(ReddackItem):
    """Stores information about the state of a comment in the modqueue."""

class ReddackSubmission(ReddackItem):
    """Stores information about the state of a submission in the modqueue."""

    _ResponseType : Type = SubmissionResponse
    kind : str = "submission"

    def __init__(self, prawitem):
        self.created_utc = prawitem.created_utc
        self.title = prawitem.title
        self.url = prawitem.url
        self.author = prawitem.author.name
        self.thumbnail = prawitem.thumbnail
        self.text = prawitem.selftext
        self.permalink = prawitem.permalink
        super().__init__(prawitem)

    def send_msg(self, client, channel, removal_options):
        """Send message for new mod item to specified Slack channel"""
        try:
            result = client.chat_postMessage(
                blocks=self.msg_payload(removal_options), channel=channel, 
                text="New modqueue item", unfurl_links=False, unfurl_media=False
            )
            result.validate()
            self.message_ts = result.data['ts']
            return result
        except SlackApiError as error:
            try:
                result = client.chat_postMessage(
                    blocks=self.msg_payload(removal_options, thumbnail=False), channel=channel, 
                    text="New modqueue item", unfurl_links=False, unfurl_media=False
                )
                result.validate()
                self.message_ts = result.data['ts']
                return result
            except SlackApiError as error:
                raise MsgSendError("Failed to send item to Slack.") from error

    def _delete_msg(self, client, user_client, channel):
        """Delete replies to mod item message"""
        response = client.conversations_replies(
            channel=channel, 
            ts=self.message_ts
        )
        for message in response["messages"][::-1]:
            reply_response = user_client.chat_delete(
                channel=channel, 
                ts=message["ts"],
                as_user=True
            )

    def _send_archive(self, client, channel):
        """Send archive message after mod actions are complete"""
        responseblocks = []
        for userid, modresponse in self.responses.items():
            response = client.users_info(user=userid)
            name = response["user"]["real_name"]
            responseblocks.append(
                build_response_block(
                    name, 
                    modresponse.states["actionApproveRemove"].value, 
                    modresponse.states["actionRemovalReason"].value
                )
            )
        archiveblocks = build_archive_blocks(
            self.created_utc, 
            self.title,
            self.author,
            self.permalink,
            responseblocks,
        )
        with open("debugdump.json", "w+") as f:
            blocksjson = jsonpickle.encode(archiveblocks)
            print(blocksjson, file=f)
        result = client.chat_postMessage(
            blocks=archiveblocks, channel=channel,
            text="Archived modqueue item", unfurl_links=False, unfurl_media=False
        )

    def complete_cleanup(self, client, user_client, channels):
        """Delete message and send to archive after completion"""
        self._send_archive(client, channels['archive'])
        self._delete_msg(client, user_client, channels['queue'])

    def initialize_response(self, moderator):
        """Initialize a new moderator response object"""
        self.responses[moderator] = self._ResponseType(self.message_ts)
    
    def approve_or_remove(self, thresholds):
        votesum = 0
        for response in self.responses.values():
            if response.actions['actionConfirm'].value: 
                votesum += float(response.states['actionApproveRemove'].value)
        if votesum >= thresholds['approve']:
            return 'approve'
        elif votesum <= thresholds['remove']:
            return "remove"
        else:
            return None
    
    def msg_payload(self, removal_options, thumbnail=True):
        try:
            return build_submission_block(
                self.created_utc, 
                self.title, 
                self.url, 
                self.author, 
                self.thumbnail if thumbnail else thumbnail,
                self.text,
                self.permalink,
                removal_options

            )
        except AttributeError as error:
            raise MsgSendError(
                f"{error.obj!r} object is missing field {error.name!r}."
            )

    @property
    def removal_reasons(self):
        unique_reasons = []
        for response in self.responses.values():
            for reason in response.states['actionRemovalReason'].value:
                if reason not in unique_reasons: unique_reasons.append(reason)
        return sorted(unique_reasons)

    @property
    def modnote(self):
        modnote = ""
        timestamp = "0"
        for response in self.responses.values():
            if response.states['actionModnote'].timestamp > timestamp:
                modnote = response.states['actionModnote'].value
        return modnote
                

class Auth(abc.ABC):
    @abc.abstractmethod
    def create_client(self):
        pass

class PrawAuth(Auth):

    AGENT = "r/SpaceX Slack moderation interface by u/ModeHopper"

    def __init__(self, client_id, client_secret, username, password):
        self.client_id = client_id
        self.client_secret = client_secret
        self.username = username
        self.password = password

    def create_client(self):
        return Reddit(
            client_id = self.client_id,
            client_secret = self.client_secret,
            password = self.password,
            username = self.username,
            user_agent = self.AGENT
        )

class SlackAuth(Auth):
    def __init__(self, bot_token, user_token):
        self.bot_token = bot_token
        self.user_token = user_token

    def create_client(self, as_user=False):
        return WebClient(token=(self.user_token if as_user else self.bot_token))

class Thresholds(TypedDict):
    approve: int
    remove: int

class Channels(TypedDict):
    queue: str
    archive: str

class RemovalTemplate(TypedDict):
    pre: str
    post: str

class Rule(TypedDict):
    title: str
    text: str
    shorttext: str
    link: str
    applyto: str

class Reddack:
    def __init__(self,
        subreddit_name: str,
        praw_auth: PrawAuth,
        slack_auth: SlackAuth,
        channels: dict[ReddackItem, Channels],
        rules: dict[str, Rule],
        known_items_path: Union[Path, str] = Path.cwd() / 'KNOWN_ITEMS.json',
        post_requests_path: Union[Path, str] = Path.cwd() / 'POST',
        thresholds: dict[ReddackItem, Thresholds] = {
            ReddackSubmission: {
                'approve': +1,
                'remove': -1
            },
            ReddackComment: {
                'approve': +1,
                'remove': -1
            }
        },
        removal_template: RemovalTemplate = None
    ):
        self.subreddit_name = subreddit_name
        self.praw_auth = praw_auth
        self.slack_auth = slack_auth
        self.known_items_path = known_items_path
        self.post_requests_path = post_requests_path
        self.rules = rules
        self.channels = channels
        self.thresholds = thresholds
        self.removal_template = removal_template
        self.removal_options = build_removal_block(self.rules)

    @property
    def subreddit(self):
        return self.praw_auth.create_client().subreddit(self.subreddit_name)

    @property
    def praw_client(self):
        return self.praw_auth.create_client()

    @property
    def slack_client(self):
        return self.slack_auth.create_client()

    @property
    def slack_user_client(self):
        return self.slack_auth.create_client(as_user=True)

    @property
    def subreddit(self):
        return self.praw_client.subreddit(self.subreddit_name)
    
    def sync(self):
        knownitems = get_known_items(self.known_items_path)
        newitems = self.check_reddit_queue(knownitems)
        knownitems = self.update_slack_queue(newitems, knownitems)
        update_knownitems_file(knownitems, self.known_items_path)
        knownitems = self.check_slack_queue(knownitems)
        update_knownitems_file(knownitems, self.known_items_path)

    def check_reddit_queue(self, knownitems):
        """Check subreddit modqueue for unmoderated items."""
        newitems = {}
        for item in self.subreddit.mod.modqueue(limit=None):
            # Check if item is comment or submission
            if isinstance(item, Submission):
                ReddackItem = ReddackSubmission
            else:
                print("Ignoring non-submission item.")
                continue
            # Check if item is known
            isknown = True if item.id in knownitems else False
            if isknown:
                continue
            else:
                newitems[item.id] = ReddackItem(item)
        return newitems

    def update_slack_queue(self, newitems, knownitems):
        for id, moditem in newitems.items():
            moditem.send_msg(
                self.slack_client, 
                self.channels[type(moditem)]['queue'],
                self.removal_options
            )
            # Add to known items
            knownitems[id] = moditem
        return knownitems
        
    def send_removal_message(self, moditem):
        body = self.removal_template["pre"]
        for removal_reason in moditem.removal_reasons:
            rule = self.rules[removal_reason]
            body +=f"\n\n>{rule['title']}: {rule['text']}"
        if moditem.modnote:
            body += f"\n\n**Moderator note:** {moditem.modnote}"
        body += "\n\n" + self.removal_template["post"]
        body += f"\n\n**Content of your {moditem.kind}**: {moditem.title}\n\n>"
        body += moditem.url if not moditem.text else moditem.text
        body += f"\n\n---\n\n[Link to your submission]({moditem.permalink})"
        subject = f"Your {moditem.kind} has been removed from r/{self.subreddit_name}"
        conversation = self.subreddit.modmail.create(subject, body, moditem.author)
        conversation.archive()
        
    def check_slack_queue(self, knownitems: dict[str, ReddackItem]):
        """Check Slack items for moderation actions"""
        incomplete = {}
        complete = {}
        if knownitems is None:
            knownitems = {}
        for moditem in knownitems.values():
            moditem.process_slack_responses(self.post_requests_path)
            if moditem.approve_or_remove(self.thresholds[type(moditem)]) == "approve":
                self.praw_client.submission(moditem.prawitem).mod.approve()
            elif moditem.approve_or_remove(self.thresholds[type(moditem)]) == "remove":
                self.praw_client.submission(moditem.prawitem).mod.remove()
                self.send_removal_message(moditem)
            else:
                incomplete[moditem.prawitem] = moditem
                continue
            complete[moditem.prawitem] = moditem
            moditem.complete_cleanup(
                self.slack_client, 
                self.slack_user_client, 
                self.channels[type(moditem)]
            )
        cleanup_knownitems_json(incomplete, self.known_items_path)
        cleanup_postrequest_json(incomplete, self.post_requests_path)
        knownitems = incomplete
        return knownitems

    