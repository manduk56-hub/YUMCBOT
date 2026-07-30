from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bot
from yumcbot import config
from yumcbot.games import bail


class BailGameTests(unittest.TestCase):
    """가라앉는 배(yumcbot/games/bail.py) 회귀 테스트."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        # patch 대상이 config인 이유는 test_bot.py setUp의 주석 참고.
        self.db_patch = patch.object(config, "DB_PATH", Path(self.temp_dir.name) / "test.sqlite3")
        self.db_patch.start()
        bot.init_db()

    def tearDown(self) -> None:
        self.db_patch.stop()
        self.temp_dir.cleanup()

    def add_user(self, user_id: str, display_name: str, score: int) -> None:
        with bot.db() as conn:
            conn.execute(
                """
                INSERT INTO users (user_id, display_name, total_score, created_at, updated_at)
                VALUES (?, ?, ?, '2026-01-01 00:00:00', '2026-01-01 00:00:00')
                """,
                (user_id, display_name, score),
            )

    def score_of(self, user_id: str) -> int:
        with bot.db() as conn:
            return int(
                conn.execute(
                    "SELECT total_score FROM users WHERE user_id = ?", (user_id,)
                ).fetchone()["total_score"]
            )

    @staticmethod
    def insert_bail_round(extra: dict) -> int:
        """가라앉는 배 라운드를 넣는다. CHECK 제약 때문에 type은 sand고 mode로 갈린다."""
        with bot.db() as conn:
            cursor = conn.execute(
                """
                INSERT INTO rounds (type, title, question, status, channel_id, extra_json, created_at)
                VALUES ('sand', '가라앉는 배', '가라앉는 배', 'open', '10', ?, '2026-01-01 00:00:00')
                """,
                (json.dumps({"mode": "bail", **extra}),),
            )
            return int(cursor.lastrowid)

    @staticmethod
    def bail_pair(**overrides) -> dict:
        pair = bot.bail_new_pair("1", "2")
        pair["proposer_id"] = "1"  # 랜덤이라 테스트에서는 고정한다
        pair.update(overrides)
        return pair

    @staticmethod
    def bail_submitted_pair(
        scoop_a: int,
        scoop_b: int,
        reward_split: dict | None = None,
    ) -> dict:
        pair = bot.bail_new_pair("1", "2")
        pair.update(
            {
                "phase": "submitting",
                "reward_split": reward_split,
                "scoop_a": scoop_a,
                "scoop_b": scoop_b,
            }
        )
        bot.resolve_bail_pair(pair)
        return pair

    @staticmethod
    def bail_even_split() -> dict:
        half = bot.BAIL_REWARD_POOL // 2
        return {"user_a": half, "user_b": bot.BAIL_REWARD_POOL - half}

    def test_bail_accepted_offer_confirms_reward_split(self) -> None:
        pair = self.bail_pair()

        bot.apply_bail_offer(pair, "1", 6)
        self.assertEqual(pair["offer"], {"proposer_id": "1", "reward": 6})
        self.assertEqual(pair["offer_count"], 1)

        bot.apply_bail_response(pair, "2", True)

        # 제안자가 6점이면 상대는 나머지를 받는다.
        self.assertEqual(pair["reward_split"], {"user_a": 6, "user_b": bot.BAIL_REWARD_POOL - 6})
        self.assertEqual(pair["phase"], "submitting")
        self.assertIsNone(pair["offer"])

    def test_bail_rejection_hands_proposal_turn_to_responder(self) -> None:
        pair = self.bail_pair()

        bot.apply_bail_offer(pair, "1", 7)
        bot.apply_bail_response(pair, "2", False)

        self.assertEqual(pair["phase"], "negotiating")
        self.assertEqual(pair["proposer_id"], "2")
        self.assertIsNone(pair["offer"])
        self.assertIsNone(pair["reward_split"])

    def test_bail_exhausted_offers_end_negotiation_in_failure(self) -> None:
        """제안 횟수를 모두 소진하면 결렬 — 감액된 보상금을 균등 분배한다."""
        pair = self.bail_pair()

        proposer, responder = "1", "2"
        for _ in range(bot.BAIL_NEGOTIATION_MAX_OFFERS):
            bot.apply_bail_offer(pair, proposer, bot.BAIL_REWARD_POOL)
            bot.apply_bail_response(pair, responder, False)
            proposer, responder = responder, proposer

        self.assertEqual(pair["offer_count"], bot.BAIL_NEGOTIATION_MAX_OFFERS)
        self.assertEqual(pair["phase"], "submitting")
        self.assertIsNone(pair["reward_split"])
        self.assertEqual(
            bot.bail_reward_share(pair, "user_a"), bot.BAIL_FAILED_REWARD_POOL // 2
        )

    def test_bail_survives_when_submissions_reach_requirement(self) -> None:
        pair = self.bail_submitted_pair(3, 3, reward_split=self.bail_even_split())

        self.assertEqual(pair["outcome"], "survived")
        self.assertEqual(pair["result"]["total_scoops"], bot.BAIL_REQUIRED_SCOOPS)
        # 공평하게 나누고 공평하게 일하면 낮은 이득: 보상 몫에서 퍼낸 만큼을 뺀다.
        half = bot.BAIL_REWARD_POOL // 2
        self.assertEqual(pair["result"]["score_a"], half - 3)
        self.assertEqual(pair["result"]["score_b"], half - 3)

    def test_bail_sink_penalty_scales_with_reward_share(self) -> None:
        """많이 받기로 한 쪽이 침몰에서 더 크게 잃는다 — 이게 배신 스위치다."""
        greedy = self.bail_submitted_pair(3, 2, reward_split={"user_a": 7, "user_b": 3})

        self.assertEqual(greedy["outcome"], "sunk")
        self.assertEqual(greedy["result"]["score_a"], -bot.BAIL_SINK_PENALTY_HIGH)
        self.assertEqual(greedy["result"]["score_b"], -bot.BAIL_SINK_PENALTY_LOW)

        even = self.bail_submitted_pair(3, 2, reward_split=self.bail_even_split())
        self.assertEqual(even["result"]["score_a"], -bot.BAIL_SINK_PENALTY_EVEN)
        self.assertEqual(even["result"]["score_b"], -bot.BAIL_SINK_PENALTY_EVEN)

    def test_bail_underpaid_side_gains_by_shirking(self) -> None:
        """보상을 적게 받은 쪽은 노동을 깎는 편이 낫다. 상대는 침몰이 더 아파서 떠안게 된다."""
        split = {"user_a": 7, "user_b": 3}
        # B가 합의대로 절반씩 일하면
        fair = self.bail_submitted_pair(3, 3, reward_split=split)
        # B가 깎고 A가 그만큼 더 떠안으면
        shirked = self.bail_submitted_pair(4, 2, reward_split=split)

        self.assertGreater(shirked["result"]["score_b"], fair["result"]["score_b"])
        # A는 침몰보다 4번 떠안는 쪽이 낫다.
        self.assertGreater(shirked["result"]["score_a"], -bot.BAIL_SINK_PENALTY_HIGH)

    def test_bail_free_rider_takes_full_share(self) -> None:
        """한 명이 전부 떠안으면 무임승차한 쪽이 몫을 그대로 챙긴다."""
        split = self.bail_even_split()
        pair = self.bail_submitted_pair(0, bot.BAIL_REQUIRED_SCOOPS, reward_split=split)

        self.assertEqual(pair["outcome"], "survived")
        self.assertEqual(pair["result"]["score_a"], split["user_a"])
        self.assertEqual(
            pair["result"]["score_b"],
            split["user_b"] - bot.BAIL_REQUIRED_SCOOPS,
        )
        # 떠안는 선택이 침몰보다 나아야 성립한다.
        self.assertGreater(pair["result"]["score_b"], -bot.BAIL_SINK_PENALTY_EVEN)

    def test_bail_failed_negotiation_is_worse_than_agreeing(self) -> None:
        """결렬은 확실히 손해다. 그래서 '합의 안 해준다'가 자기 살을 깎는 위협이 된다."""
        agreed = self.bail_submitted_pair(3, 3, reward_split=self.bail_even_split())
        failed = self.bail_submitted_pair(3, 3)

        self.assertLess(failed["result"]["score_a"], agreed["result"]["score_a"])
        self.assertEqual(
            failed["result"]["score_a"], bot.BAIL_FAILED_REWARD_POOL // 2 - 3
        )

    def test_bail_labor_is_never_bound_by_negotiation(self) -> None:
        """노동은 협상 대상이 아니다. 같은 배분이라도 제출값에 따라 점수가 달라진다."""
        split = self.bail_even_split()
        worked = self.bail_submitted_pair(4, 2, reward_split=split)
        shirked = self.bail_submitted_pair(2, 4, reward_split=split)

        self.assertNotEqual(worked["result"]["score_a"], shirked["result"]["score_a"])
        # 두 경우 모두 배는 뜬다 — 합계가 같기 때문이다.
        self.assertEqual(worked["outcome"], shirked["outcome"])

    def test_bail_autocomplete_fills_missing_submissions(self) -> None:
        pair = self.bail_pair(
            phase="submitting",
            reward_split=self.bail_even_split(),
            scoop_a=None,
            scoop_b=1,
        )

        bot.autocomplete_bail_pair(pair)

        # 미제출자만 절반으로 채우고, 이미 낸 사람은 낸 값 그대로.
        self.assertEqual(pair["scoop_a"], bot.BAIL_FALLBACK_SCOOPS)
        self.assertEqual(pair["scoop_b"], 1)
        self.assertEqual(pair["phase"], "resolved")
        self.assertTrue(pair["auto_completed"])

    def test_bail_autocomplete_during_negotiation_counts_as_failure(self) -> None:
        pair = self.bail_pair(phase="negotiating", offer={"proposer_id": "1", "reward": 8})

        bot.autocomplete_bail_pair(pair)

        self.assertEqual(pair["scoop_a"], bot.BAIL_FALLBACK_SCOOPS)
        self.assertEqual(pair["scoop_b"], bot.BAIL_FALLBACK_SCOOPS)
        self.assertIsNone(pair["reward_split"])
        self.assertEqual(pair["result"]["reward_a"], bot.BAIL_FAILED_REWARD_POOL // 2)
        self.assertIsNone(pair["offer"])
        # 절반씩이면 배가 뜬다 — 잠수 한 명이 상대까지 침몰시키지 않는다.
        self.assertEqual(pair["outcome"], "survived")

    def test_bail_negotiations_complete_flag(self) -> None:
        negotiating = {"bail_pairs": [self.bail_pair()]}
        submitting = {"bail_pairs": [self.bail_pair(phase="submitting")]}

        self.assertFalse(bot.bail_negotiations_complete(negotiating))
        self.assertTrue(bot.bail_negotiations_complete(submitting))
        # 짝이 없으면 완료가 아니다 (라운드가 조기 전이되지 않게).
        self.assertFalse(bot.bail_negotiations_complete({"bail_pairs": []}))

    def test_bail_scoop_can_be_resubmitted_until_close(self) -> None:
        """마감까지 정산이 없으므로 제출은 몇 번이든 덮어쓴다. 직전 값은 로그용으로 돌려준다."""
        pair = self.bail_pair(phase="submitting")

        self.assertIsNone(bot.apply_bail_scoop(pair, "1", 2))
        self.assertEqual(pair["scoop_a"], 2)

        self.assertEqual(bot.apply_bail_scoop(pair, "1", 5), 2)
        self.assertEqual(pair["scoop_a"], 5)
        # 번복은 본인 쪽만 건드린다.
        self.assertIsNone(pair["scoop_b"])

        self.assertIsNone(bot.apply_bail_scoop(pair, "2", 1))
        self.assertEqual(pair["scoop_b"], 1)

    def test_bail_execution_status_hides_submission_progress(self) -> None:
        """제출 인원수가 보이면 상대의 제출 여부가 새고, 번복 불가인 비공개 제출이 무너진다."""
        extra = {"bail_phase": "execution"}
        none_submitted = self.bail_pair(phase="submitting")
        one_submitted = self.bail_pair(phase="submitting", scoop_a=2)
        both_submitted = self.bail_pair(phase="submitting", scoop_a=2, scoop_b=4)

        texts = {
            bot.bail_pair_status_text(pair, extra)
            for pair in (none_submitted, one_submitted, both_submitted)
        }

        self.assertEqual(len(texts), 1)
        self.assertNotIn("/2", texts.pop())

    def test_bail_autocomplete_does_not_flag_fully_submitted_pairs(self) -> None:
        """실행 단계는 조기 정산이 없어 정상 제출한 짝도 마감 때 이 함수를 거친다."""
        pair = self.bail_pair(
            phase="submitting",
            reward_split=self.bail_even_split(),
            scoop_a=3,
            scoop_b=3,
        )

        self.assertFalse(bot.autocomplete_bail_pair(pair))

        self.assertEqual(pair["phase"], "resolved")
        self.assertNotIn("auto_completed", pair)

    def test_bail_registration_transitions_to_negotiation(self) -> None:
        self.add_user("1", "참가자1", 0)
        self.add_user("2", "참가자2", 0)
        round_id = self.insert_bail_round({"bail_phase": "registration"})
        with bot.db() as conn:
            conn.executemany(
                "INSERT INTO trust_registrations (round_id, user_id, registered_at) VALUES (?, ?, ?)",
                [(round_id, "1", "2026-01-01 00:00:00"), (round_id, "2", "2026-01-01 00:00:01")],
            )

        transition = bot.start_bail_negotiation(round_id)

        self.assertIsNotNone(transition)
        self.assertEqual(transition["state"], "negotiation")
        self.assertEqual({tuple(sorted(pair)) for pair in transition["pairs"]}, {("1", "2")})
        with bot.db() as conn:
            extra = json.loads(
                conn.execute(
                    "SELECT extra_json FROM rounds WHERE id = ?", (round_id,)
                ).fetchone()["extra_json"]
            )
        self.assertEqual(extra["bail_phase"], "negotiation")
        self.assertEqual(extra["bail_pairs"][0]["phase"], "negotiating")

    def test_bail_execution_transition_drops_pending_offers(self) -> None:
        """협상 시간이 끝나면 대기 중인 제안은 결렬로 사라진다."""
        pending = self.bail_pair(offer={"proposer_id": "1", "reward": 8}, offer_count=1)
        settled = self.bail_pair(phase="submitting", reward_split={"user_a": 7, "user_b": 3})
        round_id = self.insert_bail_round(
            {"bail_phase": "negotiation", "bail_pairs": [pending, settled]}
        )

        transition = bot.start_bail_execution(round_id)

        self.assertEqual(transition["state"], "execution")
        with bot.db() as conn:
            extra = json.loads(
                conn.execute(
                    "SELECT extra_json FROM rounds WHERE id = ?", (round_id,)
                ).fetchone()["extra_json"]
            )
        self.assertEqual(extra["bail_phase"], "execution")
        self.assertEqual(extra["bail_pairs"][0]["phase"], "submitting")
        self.assertIsNone(extra["bail_pairs"][0]["offer"])
        self.assertIsNone(extra["bail_pairs"][0]["reward_split"])
        # 이미 배분을 확정한 짝은 그대로 유지된다.
        self.assertEqual(extra["bail_pairs"][1]["reward_split"], {"user_a": 7, "user_b": 3})

    def test_bail_execution_transition_requires_negotiation_phase(self) -> None:
        round_id = self.insert_bail_round({"bail_phase": "execution", "bail_pairs": []})

        self.assertIsNone(bot.start_bail_execution(round_id))

    def test_bail_close_autocompletes_and_scores_pairs(self) -> None:
        for user_id in ("1", "2", "3", "4", "5", "6"):
            self.add_user(user_id, f"참가자{user_id}", 0)
        split = {"user_a": 7, "user_b": 3}
        survived = self.bail_submitted_pair(2, 4, reward_split=split)
        sunk = {
            **self.bail_submitted_pair(3, 2, reward_split=split),
            "user_a_id": "3",
            "user_b_id": "4",
        }
        unfinished = self.bail_pair(
            user_a_id="5",
            user_b_id="6",
            phase="submitting",
            reward_split=self.bail_even_split(),
            scoop_a=3,
        )
        round_id = self.insert_bail_round(
            {"bail_phase": "execution", "bail_pairs": [survived, sunk, unfinished]}
        )

        result = bot.close_round_record(round_id, None)

        half = bot.BAIL_REWARD_POOL // 2
        self.assertEqual(
            {user_id: self.score_of(user_id) for user_id in ("1", "2", "3", "4", "5", "6")},
            {
                # 7점 받고 2번, 3점 받고 4번 → 배는 떴다.
                "1": 7 - 2,
                "2": 3 - 4,
                # 침몰: 많이 받기로 한 3번이 더 크게 잃는다.
                "3": -bot.BAIL_SINK_PENALTY_HIGH,
                "4": -bot.BAIL_SINK_PENALTY_LOW,
                # 미제출자는 절반(3번)으로 자동 제출되어 배가 뜬다.
                "5": half - 3,
                "6": half - 3,
            },
        )
        with bot.db() as conn:
            extra = json.loads(
                conn.execute(
                    "SELECT extra_json FROM rounds WHERE id = ?", (round_id,)
                ).fetchone()["extra_json"]
            )
        self.assertEqual(extra["bail_phase"], "scored")
        self.assertTrue(extra["bail_pairs"][2]["auto_completed"])
        rendered = json.dumps(result[1].to_dict(), ensure_ascii=False)
        self.assertIn("자동 제출", rendered)
        self.assertIn("침몰", rendered)

    def test_bail_result_flags_the_free_rider(self) -> None:
        """돈은 더 받고 일은 덜 한 사람이 결과에 드러나야 한다 — 이게 평판 장치다."""
        pair = self.bail_submitted_pair(2, 4, reward_split={"user_a": 7, "user_b": 3})

        rendered = bot.bail_split_text(pair)

        self.assertIn("2번 ⚠️", rendered)  # 7점 받고 2번만 퍼냈다
        self.assertIn("4번", rendered)
        self.assertNotIn("4번 ⚠️", rendered)

    def test_bail_split_text_marks_failed_negotiation(self) -> None:
        pair = self.bail_submitted_pair(3, 3)

        rendered = bot.bail_split_text(pair)

        self.assertIn("결렬", rendered)
        self.assertNotIn("⚠️", rendered)

    def test_bail_rules_text_is_generated_from_constants(self) -> None:
        # 상수를 읽는 것은 bail 모듈이다. bot의 재노출은 사본이라 갈아끼워도 안 통한다.
        with patch.object(bail, "BAIL_SINK_PENALTY_HIGH", 9):
            self.assertIn("-9점", bot.build_bail_registration_embed(1, "배", 2).fields[1].value)

    def test_bail_pair_view_fits_discord_button_limits(self) -> None:
        """버튼은 한 줄에 5개가 상한이다. 넘기면 View 생성 자체가 터진다."""
        cases = [
            ({"bail_phase": "negotiation"}, self.bail_pair()),
            (
                {"bail_phase": "negotiation"},
                self.bail_pair(offer={"proposer_id": "1", "reward": 5}, offer_count=1),
            ),
            ({"bail_phase": "execution"}, self.bail_pair(phase="submitting")),
        ]
        for extra, pair in cases:
            view = bot.bail_pair_view(1, pair, extra)
            per_row: dict[int, int] = {}
            for item in view.children:
                per_row[item.row] = per_row.get(item.row, 0) + 1
            self.assertLessEqual(max(per_row.values()), 5, extra)
            self.assertLessEqual(len(view.children), 25, extra)
            self.assertTrue(
                all(item.custom_id.startswith("eventbot:bail") for item in view.children),
                extra,
            )

    def test_bail_pair_view_locks_proposals_while_offer_pending(self) -> None:
        extra = {"bail_phase": "negotiation"}
        pending = bot.bail_pair_view(
            1, self.bail_pair(offer={"proposer_id": "1", "reward": 5}), extra
        )

        disabled = {
            item.custom_id.rsplit(":", 1)[0]: item.disabled for item in pending.children
        }
        self.assertTrue(disabled["eventbot:bailoffer:1"])
        self.assertFalse(disabled["eventbot:bailrespond:1"])

    def test_bail_resolved_pair_has_no_buttons(self) -> None:
        view = bot.bail_pair_view(1, self.bail_submitted_pair(3, 3), {"bail_phase": "execution"})

        self.assertEqual(view.children, [])

    def test_bail_embeds_render_in_every_phase(self) -> None:
        """embed 필드는 1024자 제한이 있어 단계별로 한 번씩 만들어봐야 한다."""
        for phase, pair in (
            ("negotiation", self.bail_pair()),
            ("execution", self.bail_pair(phase="submitting", scoop_a=2)),
            ("scored", self.bail_submitted_pair(2, 4, reward_split={"user_a": 6, "user_b": 2})),
        ):
            extra = {
                "bail_phase": phase,
                "bail_pairs": [pair],
                "unmatched_user_id": "9",
                "negotiation_close_at": 100.0,
                "execution_close_at": 200.0,
            }
            round_row = {"title": "가라앉는 배", "type": "sand"}
            for embed in (
                bot.build_bail_pair_embed("가라앉는 배", pair, extra),
                bot.build_bail_play_embed(round_row, extra),
                bot.build_bail_pair_result_embed("가라앉는 배", pair),
            ):
                for field in embed.fields:
                    self.assertLessEqual(len(field.value), 1024, (phase, field.name))

    def test_bail_choices_cover_full_range(self) -> None:
        # 협상은 보상금 전부를, 실행은 노동 전부를 요구할 수 있어야 폭이 나온다.
        self.assertEqual(
            bot.BAIL_REWARD_CHOICES,
            [str(index) for index in range(bot.BAIL_REWARD_POOL + 1)],
        )
        self.assertEqual(
            bot.BAIL_SCOOP_CHOICES,
            [str(index) for index in range(bot.BAIL_REQUIRED_SCOOPS + 1)],
        )

    def test_bail_greedy_split_cannot_beat_a_moderate_one(self) -> None:
        """8:0으로 이겨도 6:2보다 나을 게 없다 — 상대가 노동할 이유를 없애면 자기도 못 챈다."""
        # 상대(B)는 몫이 0이면 퍼낼수록 손해라 0번을 낸다. A가 전부 떠안아야 배가 뜬다.
        greedy = self.bail_submitted_pair(
            bot.BAIL_REQUIRED_SCOOPS, 0, reward_split={"user_a": 8, "user_b": 0}
        )
        moderate = self.bail_submitted_pair(4, 2, reward_split={"user_a": 6, "user_b": 2})

        self.assertEqual(greedy["outcome"], "survived")
        self.assertLessEqual(greedy["result"]["score_a"], moderate["result"]["score_a"])


if __name__ == "__main__":
    unittest.main()
