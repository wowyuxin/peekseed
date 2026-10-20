import unittest

from tests.fixtures import make_dataset, sample_chunks, temp_data_root, write_chunks


class RepetitionGuardTests(unittest.TestCase):
    def test_repeated_sentences_are_detected(self):
        from peekseed.agent import RepetitionGuard
        guard = RepetitionGuard()
        piece = "The widget is mounted on the frame with two screws. " * 40
        self.assertTrue(guard.feed(piece))

    def test_normal_text_is_not_flagged(self):
        from peekseed.agent import RepetitionGuard
        guard = RepetitionGuard()
        self.assertFalse(guard.feed("The widget is mounted with two screws. "
                                    "Press the green button to start it."))


class AgentLoopTests(unittest.TestCase):
    def _tools(self):
        from peekseed import config
        from peekseed.tools import DatasetTools
        dataset = make_dataset(name="Docs")
        write_chunks(dataset, sample_chunks(dataset["id"]))
        return dataset, DatasetTools(config.paths().datasets_dir)

    def _chat(self, scripts):
        calls = {"n": 0}

        def chat(messages, tools, tool_choice):
            index = min(calls["n"], len(scripts) - 1)
            calls["n"] += 1
            for kind, piece in scripts[index]:
                yield kind, piece

        return chat

    def test_a_tool_step_then_an_answer(self):
        with temp_data_root():
            from peekseed.agent import AgentBudget, AgentSession, Ledger, build_agent_messages
            from tests.fixtures import sample_chunks as chunks
            dataset, tools = self._tools()
            excerpts = [{"n": 1, "id": dataset["id"] + "-000001",
                         "dataset": dataset["id"], "label": "2.1 Installation",
                         "section": "2.1 Installation", "page_start": 1,
                         "page_end": 1, "text": "The widget is mounted with two screws.",
                         "pdf_url": "/pdf/%s/manual.pdf#page=1" % dataset["id"]}]
            messages = build_agent_messages("How is the widget mounted?",
                                            excerpts, tools=tools.schemas())
            scripts = [
                [("tool_call_delta", [{"index": 0, "id": "call_1", "type": "function",
                                       "function": {"name": "grep",
                                                    "arguments": '{"pattern": "screws"}'}}]),
                 ("finish", "tool_calls")],
                [("text", "It is mounted with two screws [2]."), ("finish", "stop")],
            ]
            session = AgentSession(
                chat=self._chat(scripts), tools=tools,
                budget=AgentBudget(max_steps=4, max_tool_calls=4),
                dataset=dataset["id"], ledger=Ledger(excerpts))
            events = list(session.run(messages))
            kinds = [event.get("type") for event in events]
            self.assertIn("tool_call", kinds)
            self.assertIn("tool_result", kinds)
            done = [event for event in events if event.get("type") == "done"][-1]
            self.assertFalse(done["forced"])
            self.assertIn(2, done["cited"])
            self.assertGreaterEqual(len(done["ledger"]), 2)
            self.assertEqual(done["ledger"][0]["n"], 1)

    def test_a_budget_forces_a_final_answer(self):
        with temp_data_root():
            from peekseed.agent import AgentBudget, AgentSession, Ledger, build_agent_messages
            dataset, tools = self._tools()
            excerpts = [{"n": 1, "id": dataset["id"] + "-000001",
                         "dataset": dataset["id"], "label": "2.1 Installation",
                         "section": "2.1 Installation", "page_start": 1,
                         "page_end": 1, "text": "The widget is mounted with two screws.",
                         "pdf_url": ""}]
            messages = build_agent_messages("How is it mounted?", excerpts)
            scripts = [
                [("tool_call_delta", [{"index": 0, "id": "call_1", "type": "function",
                                       "function": {"name": "list_datasets",
                                                    "arguments": "{}"}}]),
                 ("finish", "tool_calls")],
                [("text", "I could not verify it further [1]."), ("finish", "stop")],
            ]
            session = AgentSession(
                chat=self._chat(scripts), tools=tools,
                budget=AgentBudget(max_steps=1, max_tool_calls=4),
                dataset=dataset["id"], ledger=Ledger(excerpts))
            events = list(session.run(messages))
            done = [event for event in events if event.get("type") == "done"][-1]
            self.assertTrue(done["forced"])
            self.assertIn("budget", (done.get("reason") or "").lower())

    def test_auto_messages_list_datasets_without_seed(self):
        from peekseed.agent import build_auto_messages
        messages = build_auto_messages("What is in these docs?", "docs",
                                       [("docs", "3 chunks")])
        self.assertEqual(messages[0]["role"], "system")
        self.assertIn("docs", messages[1]["content"])


if __name__ == "__main__":
    unittest.main()
