from __future__ import annotations

import pytest

from paperless_scan_sorter.pageplan import PageAction, PageInfo, PagePlan, plan_pages

TEXT_A = "これは同じ内容の本文です。" * 4  # 20 文字以上の本文
TEXT_B = "ABCDEFGHIJ" * 4


def _p(
    n: int,
    ink: float = 0.2,
    text: str = "通常の本文がここにあります。" * 3,
    dhash: int | None = None,
    rotate: int | None = 0,
    conf: float = 9.0,
) -> PageInfo:
    # 既定ではページごとに離れたハッシュにして、重複と判定されないようにする
    h = dhash if dhash is not None else (((1 << 32) - 1) << (n * 7 % 32)) & ((1 << 64) - 1)
    return PageInfo(n, ink, text, h, rotate, conf)


def _acts(plan: PagePlan) -> list[tuple[int, str, str]]:
    return [(a.page, a.action, a.reason) for a in plan.actions]


def test_変更が無ければ_None() -> None:
    plan = plan_pages([_p(1), _p(2), _p(3)])
    assert plan.operations is None
    assert plan.review is False
    assert plan.actions == ()


def test_白紙は削除する() -> None:
    plan = plan_pages([_p(1), _p(2, ink=0.001, text=""), _p(3)])
    assert plan.operations == [{"page": 1}, {"page": 3}]
    assert _acts(plan) == [(2, "delete", "白紙")]
    assert plan.review is False
    assert "ink=0.0010" in plan.actions[0].detail


def test_文字が少しあれば確認に回す() -> None:
    plan = plan_pages([_p(1), _p(2, ink=0.001, text="受領 2026/10/28 印"), _p(3)])
    assert plan.operations is None
    assert _acts(plan) == [(2, "review", "白紙")]
    assert plan.review is True


def test_インクが少し多ければ確認に回す() -> None:
    plan = plan_pages([_p(1), _p(2, ink=0.006, text=""), _p(3)])
    assert plan.operations is None
    assert _acts(plan) == [(2, "review", "白紙")]


def test_同じ内容の連続ページは後ろを削除する() -> None:
    plan = plan_pages([_p(1), _p(2, text=TEXT_A, dhash=0), _p(3, text=TEXT_A, dhash=0b11)])
    assert plan.operations == [{"page": 1}, {"page": 2}]
    assert _acts(plan) == [(3, "delete", "重複（2 ページ目と）")]
    assert "dist=2" in plan.actions[0].detail


def test_書式だけ同じ別ページは確認に回す() -> None:
    plan = plan_pages([_p(1, text=TEXT_A, dhash=0), _p(2, text=TEXT_B, dhash=0b11)])
    assert plan.operations is None
    assert _acts(plan) == [(2, "review", "重複（1 ページ目と）")]


def test_両方テキストなしの類似は確認に回す() -> None:
    plan = plan_pages([_p(1, text="", dhash=0), _p(2, text="", dhash=0b11)])
    assert plan.operations is None
    assert _acts(plan) == [(2, "review", "重複（1 ページ目と）")]


def test_向きが確かなら回転する() -> None:
    plan = plan_pages([_p(1, rotate=180, conf=9.0), _p(2)])
    assert plan.operations == [{"page": 1, "rotate": 180}, {"page": 2}]
    assert _acts(plan) == [(1, "rotate", "向き")]
    assert plan.actions[0].detail == "rotate=180 conf=9.0"


def test_向きが曖昧なら確認に回す() -> None:
    plan = plan_pages([_p(1, rotate=90, conf=3.0), _p(2)])
    assert plan.operations is None
    assert _acts(plan) == [(1, "review", "向き")]


def test_向きの確信が低ければ何もしない() -> None:
    plan = plan_pages([_p(1, rotate=90, conf=0.5), _p(2)])
    assert plan.operations is None
    assert plan.actions == ()
    assert plan.review is False


def test_向きが不明なら何もしない() -> None:
    plan = plan_pages([_p(1, rotate=None, conf=0.0), _p(2)])
    assert plan.operations is None


def test_白紙が_3_ページなら何も削除しない() -> None:
    pages = [_p(1), _p(2, 0.001, ""), _p(3, 0.001, ""), _p(4, 0.001, "")]
    plan = plan_pages(pages)
    assert plan.operations is None
    assert _acts(plan) == [(2, "review", "白紙"), (3, "review", "白紙"), (4, "review", "白紙")]
    assert plan.review is True


def test_1_ページだけの白紙は削除しない() -> None:
    plan = plan_pages([_p(1, 0.001, "")])
    assert plan.operations is None
    assert _acts(plan) == [(1, "review", "白紙")]


def test_全ページ白紙なら削除しない() -> None:
    plan = plan_pages([_p(1, 0.001, ""), _p(2, 0.001, "")])
    assert plan.operations is None
    assert _acts(plan) == [(1, "review", "白紙"), (2, "review", "白紙")]


def test_削除と回転は_1_回の_operations_に入る() -> None:
    plan = plan_pages([_p(1), _p(2, 0.001, ""), _p(3, rotate=270, conf=8.0)])
    assert plan.operations == [{"page": 1}, {"page": 3, "rotate": 270}]
    assert _acts(plan) == [(2, "delete", "白紙"), (3, "rotate", "向き")]


def test_削除を取りやめても回転は行う() -> None:
    pages = [_p(1, rotate=90, conf=9.0), _p(2, 0.001, ""), _p(3, 0.001, ""), _p(4, 0.001, "")]
    plan = plan_pages(pages)
    assert plan.operations == [
        {"page": 1, "rotate": 90},
        {"page": 2},
        {"page": 3},
        {"page": 4},
    ]


def test_PageAction_は値で比較できる() -> None:
    assert PageAction(1, "delete", "白紙", "x") == PageAction(1, "delete", "白紙", "x")


def test_ちょうど_2_ページの削除は行う() -> None:
    plan = plan_pages([_p(1), _p(2, 0.001, ""), _p(3, 0.001, "")])
    assert plan.operations == [{"page": 1}]
    assert _acts(plan) == [(2, "delete", "白紙"), (3, "delete", "白紙")]


def test_種類の違う削除候補が合わせて_3_ページなら全て確認に回す() -> None:
    plan = plan_pages(
        [
            _p(1),
            _p(2, 0.001, ""),
            _p(3, 0.001, ""),
            _p(4, text=TEXT_A, dhash=0),
            _p(5, text=TEXT_A, dhash=0b11),
        ]
    )
    assert plan.operations is None
    assert _acts(plan) == [
        (2, "review", "白紙"),
        (3, "review", "白紙"),
        (5, "review", "重複（4 ページ目と）"),
    ]


def test_重複は削除したページではなく直前に残すページと比べる() -> None:
    plan = plan_pages(
        [
            _p(1),
            _p(2, text=TEXT_A, dhash=0),
            _p(3, text=TEXT_A, dhash=0b11),
            _p(4, text=TEXT_A, dhash=0b111100),  # 2 とは距離 4、3 とは距離 6
        ]
    )
    assert plan.operations == [{"page": 1}, {"page": 2}]
    assert _acts(plan) == [
        (3, "delete", "重複（2 ページ目と）"),
        (4, "delete", "重複（2 ページ目と）"),
    ]


def test_白紙で確認に回したページも直前に残すページとして比べる() -> None:
    far = (1 << 32) - 1
    plan = plan_pages(
        [
            _p(1, text=TEXT_A, dhash=0),
            _p(2, ink=0.006, text="", dhash=far),
            _p(3, text=TEXT_A, dhash=far ^ 0b11),
        ]
    )
    assert plan.operations is None
    assert _acts(plan) == [(2, "review", "白紙"), (3, "review", "重複（2 ページ目と）")]


def test_ページ番号が重複していれば_ValueError() -> None:
    with pytest.raises(ValueError):
        plan_pages([_p(1, 0.001, ""), _p(1, 0.001, "")])


def test_ページが空なら何もしない() -> None:
    plan = plan_pages([])
    assert plan.operations is None
    assert plan.review is False


def test_90_180_270_以外の角度は判定不能として扱う() -> None:
    plan = plan_pages([_p(1, rotate=45, conf=9.0), _p(2, rotate=360, conf=3.0)])
    assert plan.operations is None
    assert plan.actions == ()


def test_同じ書式で_1_語だけ違うページは削除せず確認に回す() -> None:
    a = "見出しはひとつ目です。" + "共通の本文がここに入ります。" * 20
    b = "見出しはふたつ目です。" + "共通の本文がここに入ります。" * 20
    plan = plan_pages([_p(1, text=a, dhash=0), _p(2, text=b, dhash=0)])
    assert plan.operations is None
    assert _acts(plan) == [(2, "review", "重複（1 ページ目と）")]
    assert plan.review is True


def test_テキストが完全に同じなら空白が違っても削除する() -> None:
    plan = plan_pages([_p(1, text=TEXT_A, dhash=0), _p(2, text=" ".join(TEXT_A), dhash=0)])
    assert plan.operations == [{"page": 1}]
    assert _acts(plan) == [(2, "delete", "重複（1 ページ目と）")]
