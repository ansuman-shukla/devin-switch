"""Create named account entries from the user's Chrome profile index."""

from devin_switch.browser import ChromeProfile
from devin_switch.store import Account, Store


def prefix(profile: ChromeProfile) -> str:
    return "nayanshi" if "nayanshi" in f"{profile.name} {profile.email}".lower() else "ansuman"


def next_alias(profile: ChromeProfile, accounts: tuple[Account, ...]) -> str:
    names = {account.name for account in accounts}
    owner = prefix(profile)
    return next(f"{owner}-{i}" for i in range(1, len(names) + 2) if f"{owner}-{i}" not in names)


def import_profiles(store: Store, profiles: tuple[ChromeProfile, ...]) -> tuple[int, int]:
    added = 0
    renamed = 0
    for profile in profiles:
        accounts = store.accounts()
        matches = tuple(
            account for account in accounts if account.chrome_profile == profile.directory
        )
        if not matches:
            store.add(next_alias(profile, accounts), profile.directory)
            added += 1
        elif prefix(profile) == "nayanshi":
            for account in matches:
                if not account.name.startswith("nayanshi-"):
                    store.rename(account, next_alias(profile, store.accounts()))
                    renamed += 1
    return added, renamed
