"""Progress events change the status without becoming transcript content."""
import unittest
from unittest.mock import patch

from chatgpt_tui import ChatTui


class TuiProgressTests(unittest.TestCase):
    def test_status_events_are_fixed_labels_and_do_not_overwrite_streamed_text(self):
        observed = []
        def send(_cid, _prompt, _attachments, on_event, _on_connection):
            on_event({'type':'status', 'stage':'connecting', 'text':'untrusted details'})
            observed.append(tui.status)
            on_event({'type':'status', 'stage':'waiting_response'})
            observed.append(tui.status)
            on_event({'type':'delta', 'text':'fixture answer'})
            observed.append(tui.status)
            on_event({'type':'status', 'stage':'unknown', 'text':'untrusted details'})
            observed.append(tui.status)
            on_event({'type':'status', 'stage':'refreshing_conversation'})
            observed.append(tui.status)
            self.assertEqual(tui.messages[-1]['text'], 'fixture answer')
            return {'conversation_id':'fixture'}
        tui = ChatTui(lambda: [], lambda _cid: {}, send)
        tui.messages = [{'role':'user','text':'fixture'}, {'role':'assistant','text':''}]
        with patch.object(tui, 'scroll_bottom'), patch.object(tui, 'commit_completed_turns'):
            tui.run_send('fixture', [])
        self.assertEqual(observed, ['Connecting to ChatGPT…', 'Waiting for ChatGPT…',
                                   'Streaming response…', 'Streaming response…', 'Refreshing conversation…'])
        self.assertEqual(tui.status, 'Ready')
        self.assertEqual(tui.messages[-1]['text'], 'fixture answer')
