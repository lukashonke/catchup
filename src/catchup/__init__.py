"""Catchup: a local inbox that an AI agent fills with the things waiting on you."""


class CatchupError(Exception):
    """An error caused by user input or local state, with a message meant for the user."""
