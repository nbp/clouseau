import base64
import re
import pytz
from datetime import (datetime, timedelta)
try:
    from urllib.request import urlopen
except ImportError:
    from urllib import urlopen
import weakref
import numbers
import warnings
import whatthepatch
from .HGFileInfo import HGFileInfo
from .bugzilla import Bugzilla, BugzillaUser
from .CrashInfo import CrashInfo
from . import hgmozilla
from . import modules
from . import utils
from . import versions
from .connection import Query

reviewer_cache = {}


def short_name_match(short_name, real_name, email, exact_matching=True):
    short_name = short_name.lower()
    real_name = real_name.lower()

    if exact_matching:
        return ':' + short_name + ']' in real_name or\
               ':' + short_name + ')' in real_name or\
               ':' + short_name + ',' in real_name or\
               ':' + short_name + '.' in real_name or\
               ':' + short_name + ' ' in real_name
    else:
        names = real_name.split(' ')
        possible_short_name1 = names[0][0] + names[1] if names and len(names) >= 2 else ''
        possible_short_name2 = names[0] + names[1] if names and len(names) >= 2 else ''

        return (short_name in real_name) or\
               (short_name + '@mozilla.com' in real_name) or\
               (possible_short_name1 and short_name == possible_short_name1) or\
               (possible_short_name2 and short_name == possible_short_name2) or\
               (email.startswith(short_name + '@')) or\
               (short_name == email[email.index('@') + 1:email.rindex('.')])


def reviewer_match(short_name, bugzilla_names, cc_list):
    if short_name in reviewer_cache:
        if reviewer_cache[short_name] not in bugzilla_names:
            warnings.warn('Reviewer ' + reviewer_cache[short_name] + ' is not in the list of reviewers on Bugzilla.', stacklevel=3)

        return reviewer_cache[short_name]

    found = set()
    bugzilla_users = []

    # Check if we can find the reviewer in the list of reviewers from the bug.
    for bugzilla_name in bugzilla_names:
        if bugzilla_name.startswith(short_name):
            found.add(bugzilla_name)

    if len(found) == 0:
        # Otherwise, check if we can find him/her in the CC list.
        found |= set([entry['email'] for entry in cc_list if short_name_match(short_name, entry['real_name'], entry['email'])])

    if len(found) == 0:
        # Otherwise, find matching users on Bugzilla.
        def user_handler(u):
            bugzilla_users.append(u)

        INCLUDE_FIELDS = ['email', 'real_name']

        BugzillaUser(search_strings='match=' + short_name + '&include_fields=' + ','.join(INCLUDE_FIELDS), user_handler=user_handler).wait()
        for bugzilla_name in bugzilla_names:
            BugzillaUser(bugzilla_name, include_fields=INCLUDE_FIELDS, user_handler=user_handler).wait()

        found |= set([user['email'] for user in bugzilla_users if short_name_match(short_name, user['real_name'], user['email'])])

    if len(found) == 0:
        # Otherwise, check if we can find him/her in the CC list with a relaxed matching algorithm.
        found |= set([entry['email'] for entry in cc_list if short_name_match(short_name, entry['real_name'], entry['email'], False)])

    # We should always find a matching reviewer name.
    # If we're unable to find it, add a static entry in the
    # reviewer_cache dict or find a new clever way to retrieve it.
    if len(found) == 0:
        warnings.warn('Reviewer ' + short_name + ' could not be found.', stacklevel=3)
        return None

    for elem in found:
        if elem not in bugzilla_names:
            warnings.warn('Reviewer ' + elem + ' is not in the list of reviewers on Bugzilla.', stacklevel=3)

    assert len(found) <= 1, 'Too many matching reviewers (' + ', '.join(found) + ') found for ' + short_name

    assert short_name not in reviewer_cache
    reviewer_cache[short_name] = found.pop()
    return reviewer_cache[short_name]


def author_match(author_mercurial, author_real_name, bugzilla_names, cc_list):
    if author_mercurial in bugzilla_names:
        return set([author_mercurial])

    found = set()

    if len(bugzilla_names) == 1:
        found.add(list(bugzilla_names)[0])

    # Check in the cc_list, so we don't have to hit Bugzilla.
    for entry in cc_list:
        if author_real_name in entry['real_name']:
            found.add(entry['email'])

    if len(found) == 0:
        # Otherwise, search on Bugzilla.
        bugzilla_users = []

        def user_handler(u):
            bugzilla_users.append(u)

        INCLUDE_FIELDS = ['email', 'real_name']

        BugzillaUser(search_strings='match=' + author_real_name + '&include_fields=' + ','.join(INCLUDE_FIELDS), user_handler=user_handler).wait()
        for user in bugzilla_users:
            if author_real_name in user['real_name']:
                found.add(user['email'])

    if len(found) == 0:
        warnings.warn('Author ' + author_mercurial + ' could not be found.', stacklevel=3)
        return set([])

    for elem in found:
        if elem not in bugzilla_names:
            warnings.warn('Author ' + elem + ' is not in the list of authors on Bugzilla.', stacklevel=3)

    assert len(found) <= 1, 'Too many matching authors (' + ', '.join(found) + ') found for ' + author_mercurial

    return set([author_mercurial, found.pop()])


def _is_test(path):
    return 'test' in path and not path.endswith(('ini', 'list', 'in', 'py', 'json', 'manifest'))


def get_user_details(username):
    output = {}

    def user_handler(user):
        output.update(user)

    BugzillaUser(username, user_handler=user_handler).wait()

    return output


hginfos = weakref.WeakValueDictionary()


def patch_analysis(patch, authors, reviewers, creation_date=utils.get_date_ymd('today')):
    info = {
        'changes_size': 0,
        'test_changes_size': 0,
        'modules_num': 0,
        'code_churn_overall': 0,
        'code_churn_last_3_releases': 0,
        'developer_familiarity_overall': 0,
        'developer_familiarity_last_3_releases': 0,
        'reviewer_familiarity_overall': 0,
        'reviewer_familiarity_last_3_releases': 0,
        'crashes': 0,
    }

    paths = []
    for diff in whatthepatch.parse_patch(patch):
        old_path = diff.header.old_path[2:] if diff.header.old_path.startswith('a/') else diff.header.old_path
        new_path = diff.header.new_path[2:] if diff.header.new_path.startswith('b/') else diff.header.new_path

        # TODO: Split C/C++, Rust, Java, JavaScript, build system changes
        if _is_test(new_path):
            info['test_changes_size'] += len(diff.changes)
        else:
            info['changes_size'] += len(diff.changes)

        if old_path != '/dev/null' and old_path != new_path:
            paths.append(old_path)

        if new_path != '/dev/null':
            paths.append(new_path)

    used_modules = {}
    ci = CrashInfo(paths).get()  # TODO: Only check files that can actually be here (.c or .cpp).
    for path in paths:
        info['crashes'] += ci[path]

        module = modules.module_from_path(path)
        if module and module['name'] not in used_modules:
            used_modules[module['name']] = 1

        if path in hginfos:
            hi = hginfos[path]
        else:
            hi = hginfos[path] = HGFileInfo(path, date_type='creation')

        utc_ts_to = utils.get_timestamp(creation_date) - 1  # -1 so it doesn't include the current patch

        info['code_churn_overall'] += len(hi.get(path, utc_ts_to=utc_ts_to)['patches'])
        info['code_churn_last_3_releases'] += len(hi.get(path, utc_ts_from=utils.get_timestamp(creation_date + timedelta(-3 * 6 * 7)), utc_ts_to=utc_ts_to)['patches'])
        info['developer_familiarity_overall'] += len(hi.get(path, authors=authors, utc_ts_to=utc_ts_to)['patches'])
        info['developer_familiarity_last_3_releases'] += len(hi.get(path, authors=authors, utc_ts_from=utils.get_timestamp(creation_date + timedelta(-3 * 6 * 7)), utc_ts_to=utc_ts_to)['patches'])
        info['reviewer_familiarity_overall'] += len(hi.get(path, authors=reviewers, utc_ts_to=utc_ts_to)['patches'])
        info['reviewer_familiarity_last_3_releases'] += len(hi.get(path, authors=reviewers, utc_ts_from=utils.get_timestamp(creation_date + timedelta(-3 * 6 * 7)), utc_ts_to=utc_ts_to)['patches'])

    info['modules_num'] = sum(used_modules.values())

    # TODO: Add coverage info before and after the patch.

    return info


MOZREVIEW_URL_PATTERN = 'https://reviewboard.mozilla.org/r/([0-9]+)/'


# TODO: Consider feedback+ and feedback- as review+ and review-
def bug_analysis(bug, uplift_channel='release'):
    if isinstance(bug, numbers.Number):
        bug_id = bug
        bug = {}

        def bughandler(found_bug, data):
            bug.update(found_bug)

        def commenthandler(found_bug, bugid, data):
            bug['comments'] = found_bug['comments']

        def attachmenthandler(attachments, bugid, data):
            bug['attachments'] = attachments

        def historyhandler(found_bug, data):
            bug['history'] = found_bug['history']

        INCLUDE_FIELDS = [
            'id', 'flags', 'depends_on', 'keywords', 'blocks', 'whiteboard', 'resolution', 'status',
            'url', 'version', 'summary', 'priority', 'product', 'component', 'severity',
            'platform', 'op_sys', 'cc',
            'assigned_to', 'creator',
        ]

        ATTACHMENT_INCLUDE_FIELDS = [
            'flags', 'is_patch', 'creator', 'content_type',
        ]

        Bugzilla(bug_id, INCLUDE_FIELDS, bughandler=bughandler, commenthandler=commenthandler, attachmenthandler=attachmenthandler, historyhandler=historyhandler, attachment_include_fields=ATTACHMENT_INCLUDE_FIELDS).get_data().wait()

    info = {
        'backout_num': 0,
        'blocks': len(bug['blocks']),
        'depends_on': len(bug['depends_on']),
        'comments': len(bug['comments']),
        'r-ed_patches': sum((a['is_patch'] == 1 or a['content_type'] == 'text/x-review-board-request') and sum(flag['name'] == 'review' and flag['status'] == '-' for flag in a['flags']) > 0 for a in bug['attachments']),
    }

    # Store bug creator & assignee
    assignee = bug.get('assigned_to_detail')
    creator = bug.get('creator_detail')

    # Get all reviewers and authors, we will match them with the changeset description (r=XXX).
    bugzilla_reviewers = set()
    bugzilla_authors = set()
    for attachment in bug['attachments']:
        if sum(flag['name'] == 'review' and (flag['status'] == '+' or flag['status'] == '-') for flag in attachment['flags']) == 0:
            continue

        bugzilla_authors.add(attachment['creator'])

        for flag in attachment['flags']:
            # If the creator of the patch is the setter of the review flag, it's probably
            # because he/she was carrying a r+, so we don't add him/her to the reviewers list.
            if flag['setter'] == attachment['creator']:
                continue

            bugzilla_reviewers.add(flag['setter'])

    reviewer_pattern = re.compile('r=([a-zA-Z0-9]+)')
    author_pattern = re.compile('<([^>]+)>')
    email_pattern = re.compile('<?([\w\-\._\+%]+@[\w\-\._\+%]+)>?')
    backout_pattern = re.compile('(?:backout|back out|backed out|backedout) (?:changeset )?([a-z0-9]{12,})')
    bug_pattern = re.compile('[\t ]*bug[\t ]*([0-9]+)')
    landings = Bugzilla.get_landing_comments(bug['comments'], ['inbound', 'central', 'fx-team'])
    revs = {}
    backed_out_revs = set()
    backout_comments = set()
    for landing in landings:
        rev = landing['revision'][:12]
        channel = landing['channel']

        # TODO: No need to get the revision, we have everything in the raw format.
        #       We can use pylib/mozautomation/mozautomation/commitparser.py from version-control-tools
        # Or maybe it's better this way, so we can avoid downloading a lot of changes when it's unneeded
        # to do so (e.g. for backouts or merges we only need the description).
        meta = hgmozilla.Revision.get_revision(channel, rev)
        if not meta:
            warnings.warn('Revision ' + rev + ' doesn\'t exist.', stacklevel=2)
            continue
        meta['desc'] = meta['desc'].lower()

        # Check if it was a backout
        backout_revisions = set()
        for match in backout_pattern.finditer(meta['desc']):
            backout_revisions.add(match.group(1)[:12])

        # TODO: Improve matching a backout of multiple changesets in a single line (e.g. bug 683280).
        if not backout_revisions:
            match = re.search('(?:backout|back out|backed out|backedout) changesets', meta['desc'])
            if match:
                pattern = re.compile('([a-z0-9]{12,})')
                for match in pattern.finditer(meta['desc']):
                    backout_revisions.add(match.group(1)[:12])

        if not backout_revisions:
            match = re.search('backout|back out|backed out|backedout', meta['desc'])
            if match:
                for parent in meta['parents']:
                    for match in backout_pattern.finditer(hgmozilla.Revision.get_revision(channel, parent)['desc'].lower()):
                        backout_revisions.add(match.group(1)[:12])

                # It's definitely a backout, but we couldn't find which revision was backed out.
                if not backout_revisions:
                    warnings.warn(rev + ' looks like a backout, but we couldn\'t find which revision was backed out.', stacklevel=2)
                # I wish we could assert instead of warn.
                # assert backout_revisions

        if backout_revisions and not backout_revisions.issubset(backed_out_revs):
            backout_comments.add(landing['comment']['id'])
            backed_out_revs.update(backout_revisions)

        if backout_revisions:
            continue

        bug_id_match = re.search(bug_pattern, meta['desc'])
        if bug_id_match:
            if int(bug_id_match.group(1)) != bug['id']:
                warnings.warn('Revision ' + rev + ' is related to another bug (' + bug_id_match.group(1) + ').', stacklevel=2)
                continue

        # Skip merges (e.g. http://hg.mozilla.org/mozilla-central/rev/4ca898d7db5f from 914034)
        if not bug_id_match and 'merge' in meta['desc']:
            continue

        reviewers = set()
        for match in reviewer_pattern.finditer(meta['desc']):
            reviewers.add(match.group(1))

        author_mercurial_match = author_pattern.search(meta['user'])
        if author_mercurial_match is None:
            author_mercurial_match = email_pattern.search(meta['user'])
        author_mercurial = author_mercurial_match.group(1)
        author_real_name = meta['user'][:author_mercurial_match.start() - 1]
        # Multiple names because sometimes authors use different emails on Bugzilla and Mercurial and sometimes
        # they change it.
        author_names = author_match(author_mercurial, author_real_name, bugzilla_authors, bug['cc_detail'])

        # Overwrite revisions from integration channels (inbound, fx-team).
        if rev not in revs or channel == 'central':
            revs[rev] = {
                'channel': channel,
                'author_names': author_names,
                'creation_date': meta['date'][0],
                'reviewers': reviewers,
            }

    # Remove backed out changesets
    for rev in backed_out_revs:
        if rev not in revs:
            warnings.warn('Revision ' + rev + ' was not found.', stacklevel=2)
        else:
            del revs[rev]

    if len(revs) > 0:
        for rev, obj in revs.items():
            reviewers = set()

            short_reviewers = obj['reviewers']

            for short_reviewer in short_reviewers:
                # TODO: Figure out if this is the best thing to do (probably we should just skip these and not add the author
                # to the set of reviewers).
                if short_reviewer in ['me', 'oops', 'none', 'bustage', 'backout']:
                    reviewers |= obj['author_names']
                else:
                    reviewers.add(reviewer_match(short_reviewer, bugzilla_reviewers | bugzilla_authors, bug['cc_detail']))

            obj['reviewers'] = reviewers

            patch_info = patch_analysis(hgmozilla.RawRevision.get_revision(obj['channel'], rev), obj['author_names'], reviewers, datetime.utcfromtimestamp(obj['creation_date']))
            for k, v in patch_info.items():
                if k not in info:
                    info[k] = v
                else:
                    info[k] += v
    else:
        def attachmenthandler(attachments, bugid, data):
            for i in range(0, len(bug['attachments'])):
                bug['attachments'][i].update(attachments[i])

        Bugzilla(bug['id'], attachmenthandler=attachmenthandler, attachment_include_fields=['data', 'is_obsolete', 'creation_time']).get_data().wait()

        for attachment in bug['attachments']:
            if sum(flag['name'] == 'review' and flag['status'] == '+' for flag in attachment['flags']) == 0:
                continue

            data = None

            if attachment['is_patch'] == 1 and attachment['is_obsolete'] == 0:
                data = base64.b64decode(attachment['data']).decode('ascii', 'ignore')
            elif attachment['content_type'] == 'text/x-review-board-request' and attachment['is_obsolete'] == 0:
                mozreview_url = base64.b64decode(attachment['data']).decode('utf-8')
                review_num = re.search(MOZREVIEW_URL_PATTERN, mozreview_url).group(1)
                mozreview_raw_diff_url = 'https://reviewboard.mozilla.org/r/' + review_num + '/diff/raw/'

                response = urlopen(mozreview_raw_diff_url)
                data = response.read().decode('ascii', 'ignore')

            reviewers = [flag['setter'] for flag in attachment['flags'] if flag['name'] == 'review' and flag['status'] == '+']

            if data is not None:
                patch_info = patch_analysis(data, [attachment['creator']], reviewers, utils.get_date_ymd(attachment['creation_time']))
                for k, v in patch_info.items():
                    if k not in info:
                        info[k] = v
                    else:
                        info[k] += v

    # TODO: Add number of crashes with signatures from the bug (also before/after the patch?).

    # TODO: Add perfherder results?

    # TODO: Add number of days since the landing (to check if the patch baked a little on nightly or not).

    info['backout_num'] = len(backout_comments)

    # Add users
    info['users'] = {
        'creator': creator,
        'assignee': assignee,
        'authors': bugzilla_authors,
        'reviewers': bugzilla_reviewers,
    }

    # Add uplift request
    info.update(uplift_info(bug, uplift_channel))

    return info


def uplift_info(bug, channel):
    if isinstance(bug, numbers.Number):
        bug_id = bug
        bug = {}

        def bughandler(found_bug, data):
            bug.update(found_bug)

        def commenthandler(found_bug, bugid, data):
            bug['comments'] = found_bug['comments']

        def historyhandler(found_bug, data):
            bug['history'] = found_bug['history']

        def attachmenthandler(attachments, bugid, data):
            bug['attachments'] = attachments

        INCLUDE_FIELDS = [
            'id',
        ]

        ATTACHMENT_INCLUDE_FIELDS = [
            'flags',
        ]

        Bugzilla(bug_id, INCLUDE_FIELDS, bughandler=bughandler, commenthandler=commenthandler, historyhandler=historyhandler, attachmenthandler=attachmenthandler, attachment_include_fields=ATTACHMENT_INCLUDE_FIELDS).get_data().wait()

    # Default structure
    info = {
        'uplift_accepted': False,
        'uplift_comment': None,
        'uplift_author': None,
        'landing_delta': timedelta(),
        'response_delta': timedelta(),
        'release_delta': timedelta(),
    }
    approval_flag = 'approval-mozilla-' + channel

    app_flags = filter(lambda f: f['name'] == approval_flag, [flag for a in bug['attachments'] for flag in a['flags']])
    status = [flag['status'] for flag in app_flags]
    uplift_accepted = any(filter(lambda s: s == '+', status))
    uplift_rejected = any(filter(lambda s: s == '-', status))

    assert not (uplift_accepted and uplift_rejected), 'Uplift either accepted or rejected.'

    info['uplift_accepted'] = uplift_accepted

    # Delta between uplift request and uplift acceptation/rejection.
    uplift_request = Bugzilla.get_history_matches(bug['history'], {'added': approval_flag + '?', 'field_name': 'flagtypes.name'})
    uplift_pattern = re.compile('Approval Request Comment')
    if len(uplift_request):
        uplift_request_date = utils.get_date_ymd(uplift_request[-1]['when'])
    else:
        uplift_request_date = 0
        warnings.warn('Bug ' + str(bug['id']) + ' doesn\'t have a uplift request date.', stacklevel=2)

    sign = '+' if uplift_accepted else '-'
    uplift_response = Bugzilla.get_history_matches(bug['history'], {'added': approval_flag + sign, 'field_name': 'flagtypes.name'})

    if uplift_response:
        uplift_response_date = utils.get_date_ymd(uplift_response[-1]['when'])
        if uplift_request_date == 0:
            uplift_request_date = uplift_response_date
        info['response_delta'] = uplift_response_date - uplift_request_date
        assert info['response_delta'] >= timedelta()

    # Search the uplift request comment
    for comment in bug['comments']:
        for match in uplift_pattern.finditer(comment['text']):
            # Use first one only
            info['uplift_author'] = get_user_details(comment['author'])
            info['uplift_comment'] = comment
            break
        if info['uplift_comment'] and info['uplift_author']:
            break

    if uplift_request_date == 0:
        # No deltas calculations
        return info

    # Delta between uplift request and next merge date.
    release_date = versions.getCloserMajorRelease(uplift_request_date)[1]
    info['release_delta'] = release_date - uplift_request_date
    assert info['release_delta'] > timedelta()

    # Delta between patch landing on central and uplift request
    landing_comments = Bugzilla.get_landing_comments(bug['comments'], 'central')
    if landing_comments:
        landing_date = utils.get_date_ymd(landing_comments[-1]['comment']['time'])
        info['landing_delta'] = uplift_request_date - landing_date
        # Sometimes the request is done earlier than landing on central.
        # assert bug_data['landing_delta'] > timedelta()
    else:
        info['landing_delta'] = timedelta()

    return info


def get_patch_info(bugs, base_versions=None):
    channels = ['release', 'beta', 'aurora', 'nightly']
    landing_patterns = Bugzilla.get_landing_patterns(channels=channels)
    approval_pattern = re.compile('approval-mozilla-([a-zA-Z0-9]+)\+')

    def comment_handler(bug, bugid, data):
        r = Bugzilla.get_landing_comments(bug['comments'], [], landing_patterns)
        bugid = str(bugid)
        if r:
            d = {}
            for i in r:
                revision = i['revision']
                channel = i['channel']

                dr = {'date': None, 'backedout': False, 'bugid': bugid}
                if channel in d:
                    if revision not in d[channel]:
                        d[channel][revision] = dr
                else:
                    d[channel] = {revision: dr}

            data[bugid]['land'] = d

    def history_handler(_history, data):
        bugid = str(_history['id'])
        history = _history['history']
        approval = set()
        if history:
            for changes in history:
                for change in changes['changes']:
                    field_name = change.get('field_name', None)
                    if field_name == 'flagtypes.name':
                        if 'added' in change:
                            for m in approval_pattern.finditer(change['added']):
                                approval.add(m.group(1))
                        if 'removed' in change:
                            for m in approval_pattern.finditer(change['removed']):
                                approval.discard(m.group(1))

        data[bugid]['approval'] = approval

    info = {str(bugid): {'land': None, 'approval': None, 'affected': set(), 'signatures': []} for bugid in bugs}
    status_flags = Bugzilla.get_status_flags(base_versions)
    status_flags = {c: status_flags[c] for c in channels}

    toremove = set()

    def bug_handler(bug, data):
        bugid = str(bug['id'])
        data[bugid]['signatures'] = utils.signatures_parser(bug.get('cf_crash_signature', None))
        for chan, flag in status_flags.items():
            if flag in bug:
                if bug[flag] == 'affected':
                    data[bugid]['affected'].add(chan)
            else:
                # Bug for thunderbird or anything else except Firefox
                toremove.add(bugid)

    Bugzilla(bugs, include_fields=['id', 'cf_crash_signature'] + status_flags.values(), bughandler=bug_handler, bugdata=info, commenthandler=comment_handler, commentdata=info, historyhandler=history_handler, historydata=info).get_data().wait()

    for r in toremove:
        del info[r]

    toremove.clear()
    queries = []

    bug_pattern = re.compile('[\t ]*[Bb][Uu][Gg][\t ]*([0-9]+)')

    def handler_revision(json, data):
        data['date'] = pytz.utc.localize(datetime.utcfromtimestamp(json['date'][0]))
        data['backedout'] = json.get('backedoutby', '') != ''
        m = bug_pattern.search(json['desc'])
        if not m or m.group(1) != data['bugid']:
            data['bugid'] = ''

    for bugid, i in info.items():
        land = i['land']
        if land:
            # we need to check that patches haven't been backed out
            # so prepare query for mercurial
            approval = i['approval']
            chan_toremove = set()
            for chan, revs in land.items():
                if chan == 'nightly' or chan in approval:
                    url = hgmozilla.Revision.get_url(chan)
                    for rev_num, rev_info in revs.items():
                        queries.append(Query(url, {'node': rev_num}, handler_revision, rev_info))
                else:
                    # no approval
                    chan_toremove.add(chan)
            for c in chan_toremove:
                del land[c]
            if not land:
                toremove.add(bugid)
        else:  # nothing landed so useless...
            toremove.add(bugid)

    if queries:
        hgmozilla.Revision(queries=queries).wait()

    for r in toremove:
        del info[r]

    toremove.clear()

    guttemberg = utils.get_guttenberg_death()
    for bugid, i in info.items():
        for chan, revs in i['land'].items():
            last_date = guttemberg
            for rev_num, rev_info in revs.items():
                if not rev_info['backedout'] and rev_info['bugid'] != '':
                    rev_date = rev_info['date']
                    if last_date < rev_date:
                        last_date = rev_date
            if last_date != guttemberg:
                i['land'][chan] = last_date
            else:
                toremove.add(bugid)

    for r in toremove:
        del info[r]

    return info
