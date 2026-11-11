"""``did you mean`` for a command line that was mis-typed.

Every tool on the stack - ``xrd-cp``, ``xrd-fs``, ``xrd-proxy``, the twenty-one
``gfal-*`` commands, ``xrdarc`` and ``xrdwms`` - parses with
:class:`xrdcore.report.Parser`, so one reading of "what did they probably
mean" reaches all of them, and each tool's ``--help`` stays the only thing it
has to keep true.

Two things ``argparse`` reports as usage errors are really typos, and both
know the set of answers:

* an option it does not know - ``unrecognized arguments: -vaild 1:00`` - is
  answered from the options it does know (``; did you mean '-valid'?``);
* a value outside a choice set - ``invalid choice: 'inti'`` for a subcommand,
  or ``'arcx'`` for ``--profile`` - is answered from that set.

Nothing is suggested unless it is *one or two keystrokes* from a name the
tool accepts (:data:`EDITS`): a name that merely shares letters is not
offered, because a wrong suggestion costs more than none - ``--no-such-option``
must not be answered with ``--no-authentication``. A name of fewer than three
characters is never guessed at either (``-k`` is not a typo of ``-l``), and a
stray positional argument gets nothing, since there is no list it came from.
The suggestion goes after argparse's own text, which is left as it was, so
anything that parses that text keeps working.
"""

from __future__ import annotations

import argparse
import re
from collections.abc import Sequence

__all__ = ["EDITS", "closest", "suggestion"]

#: How many single-character edits away a name may be and still be offered:
#: one for a name up to :data:`LONG` characters, one more past that, which is
#: what a fat finger looks like on a long option. A transposition (``vaild``
#: for ``valid``) costs two edits, as a transposition is two substitutions;
#: ``inti``/``init``, ``-cers``/``-cert`` and ``submitt``/``submit`` are all
#: inside the limit, while ``--no-such-option`` is eight edits from
#: ``--no-authentication`` and so is not guessed at.
EDITS = 2

#: A name longer than this may be :data:`EDITS` + 1 edits away.
LONG = 7

#: What ``argparse`` says when it does not know an argument: everything after
#: this is the words it refused.
_UNRECOGNISED = "unrecognized arguments: "

#: What it says when a value is outside a choice set; the word is in quotes.
_INVALID_CHOICE = re.compile(r"invalid choice: '([^']*)'")


def suggestion(parser: argparse.ArgumentParser, message: str) -> str:
    """``; did you mean '-valid'?`` when ``message`` is a mis-typed name, else ``""``.

    ``message`` is the text ``argparse`` handed :meth:`ArgumentParser.error`,
    and ``parser`` is the one that refused - a subparser for its own options
    and values, the top-level parser for unknown ones before a command.
    """
    for word, accepted in _complaints(parser, message):
        match = closest(word, accepted)
        if match is not None:
            return f"; did you mean {match!r}?"
    return ""


def _complaints(parser: argparse.ArgumentParser, message: str) -> list[tuple[str, list[str]]]:
    """What was mis-typed and what it could have been, for each word in the error."""
    if message.startswith(_UNRECOGNISED):
        words = message[len(_UNRECOGNISED) :].split()
        return [(_named(word), _options(parser)) for word in words if word.startswith("-")]
    choice = _INVALID_CHOICE.search(message)
    if choice is not None:
        return [(choice.group(1), _choices(parser))]
    return []


def _named(word: str) -> str:
    """The option in a command-line word: ``--profile=arc`` is ``--profile``."""
    return word.split("=", 1)[0]


def _options(parser: argparse.ArgumentParser) -> list[str]:
    """Every option string the parser accepts, its subcommands' included.

    ``parser._actions`` is ``argparse``'s own record of what it registered;
    the chain already reads it for the help headings, and it is the only place
    the option strings exist after the arguments are added.

    Subcommands are walked as well because a subparser hands the words it does
    not know back to the top-level parser, which is what reports them: for
    ``xrd-proxy init -vaild 1:00`` the refusing parser is ``xrd-proxy`` and
    the option to offer, ``-valid``, is registered on ``init``.
    """
    options: list[str] = []
    pending = [parser]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:  # the same parser registered twice
            continue
        seen.add(id(current))
        options.extend(option for action in current._actions for option in action.option_strings)
        pending.extend(
            action.choices[name]
            for action in current._actions
            if isinstance(action, argparse._SubParsersAction)
            for name in action.choices
        )
    return options


def _choices(parser: argparse.ArgumentParser) -> list[str]:
    """Every value a choice argument of the parser accepts, its commands included."""
    return [
        str(choice) for action in parser._actions if action.choices for choice in action.choices
    ]


def closest(word: str, accepted: Sequence[str]) -> str | None:
    """The one name in ``accepted`` that ``word`` probably meant, or ``None``.

    For a tool that reads a word of its own out of the command line rather
    than through ``argparse``'s choices - ``xrd-cp``'s ``--tpc delegate``.


    The nearest by edit distance, when it is within the limit for a word this
    long; failing that, a name one of them is a prefix of the other
    (``submitt`` for ``sub``, ``--lon`` for ``--long``). A name of fewer than
    three characters once the dashes are off is never offered, since every
    one-letter option is one edit from every other.
    """
    if len(word.lstrip("-")) < 3:
        return None
    limit = EDITS if len(word) <= LONG else EDITS + 1
    best: tuple[int, str] | None = None
    for candidate in accepted:
        score = _score(word, candidate, limit)
        if score is not None and (best is None or score < best[0]):
            best = (score, candidate)
    return None if best is None else best[1]


def _score(word: str, candidate: str, limit: int) -> int | None:
    """How well ``candidate`` answers ``word`` - lower is better, ``None`` is not at all."""
    if len(candidate.lstrip("-")) < 3:
        return None
    distance = _distance(word, candidate)
    if distance <= limit:
        return distance
    short, longer = sorted((word.lstrip("-"), candidate.lstrip("-")), key=len)
    if longer.startswith(short):
        return limit + 1  # a short form of the same name: behind any real spelling fix
    return None


def _distance(left: str, right: str) -> int:
    """Levenshtein distance: substitutions, insertions and deletions, each costing 1."""
    previous = list(range(len(right) + 1))
    for row, character in enumerate(left, 1):
        current = [row]
        for column, other in enumerate(right, 1):
            current.append(
                min(
                    previous[column] + 1,
                    current[column - 1] + 1,
                    previous[column - 1] + (character != other),
                )
            )
        previous = current
    return previous[-1]
