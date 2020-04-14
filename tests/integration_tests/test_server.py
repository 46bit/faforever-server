import asyncio
import contextlib

import pytest
from server.db.models import avatars, avatars_list, ban
from sqlalchemy import and_, select
from tests.utils import fast_forward

from .conftest import (
    connect_and_sign_in, connect_client, perform_login, read_until,
    read_until_command
)

pytestmark = pytest.mark.asyncio
TEST_ADDRESS = ('127.0.0.1', None)


async def test_server_deprecated_client(lobby_server):
    proto = await connect_client(lobby_server)

    await proto.send_message({'command': 'ask_session', 'user_agent': 'faf-client', 'version': '0.0.0'})
    msg = await proto.read_message()

    assert msg['command'] == 'notice'

    proto = await connect_client(lobby_server)
    await proto.send_message({'command': 'ask_session', 'version': '0.0.0'})
    msg = await proto.read_message()

    assert msg['command'] == 'notice'


@fast_forward(50)
async def test_ping_message(lobby_server):
    _, _, proto = await connect_and_sign_in(('test', 'test_password'), lobby_server)

    # We should receive the message every 45 seconds
    await read_until_command(proto, 'ping', timeout=46)


@fast_forward(5)
async def test_player_info_broadcast(lobby_server):
    p1 = await connect_client(lobby_server)
    p2 = await connect_client(lobby_server)

    await perform_login(p1, ('test', 'test_password'))
    await perform_login(p2, ('Rhiza', 'puff_the_magic_dragon'))

    await read_until(
        p2, lambda m: 'player_info' in m.values()
        and any(map(lambda d: ('login', 'test') in d.items(), m['players']))
    )


@fast_forward(5)
async def test_info_broadcast_authenticated(lobby_server):
    proto1 = await connect_client(lobby_server)
    proto2 = await connect_client(lobby_server)
    proto3 = await connect_client(lobby_server)

    await perform_login(proto1, ('test', 'test_password'))
    await perform_login(proto2, ('Rhiza', 'puff_the_magic_dragon'))
    await proto1.send_message({
        "command": "game_matchmaking",
        "state": "start",
        "mod": "ladder1v1",
        "faction": "uef"
    })
    # Will timeout if the message is never received
    await read_until_command(proto2, "matchmaker_info")
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(proto3.read_message(), 0.2)
        # Unauthenticated connections should not receive the message
        assert False


@fast_forward(5)
async def test_game_info_not_broadcast_to_foes(lobby_server):
    # Rhiza is foed by test
    _, _, proto1 = await connect_and_sign_in(
        ("test", "test_password"), lobby_server
    )
    _, _, proto2 = await connect_and_sign_in(
        ("Rhiza", "puff_the_magic_dragon"), lobby_server
    )
    await read_until_command(proto1, "game_info")
    await read_until_command(proto2, "game_info")

    await proto1.send_message({
        "command": "game_host",
        "title": "No Foes Allowed",
        "mod": "faf",
        "visibility": "public"
    })

    msg = await read_until_command(proto1, "game_info")

    assert msg["featured_mod"] == "faf"
    assert msg["title"] == "No Foes Allowed"
    assert msg["visibility"] == "public"

    with pytest.raises(asyncio.TimeoutError):
        await read_until_command(proto2, "game_info", timeout=1)


@fast_forward(5)
async def test_game_info_broadcast_to_friends(lobby_server):
    # test is the friend of friends
    _, _, proto1 = await connect_and_sign_in(
        ("friends", "friends"), lobby_server
    )
    _, _, proto2 = await connect_and_sign_in(
        ("test", "test_password"), lobby_server
    )
    _, _, proto3 = await connect_and_sign_in(
        ("Rhiza", "puff_the_magic_dragon"), lobby_server
    )
    await read_until_command(proto1, "game_info")
    await read_until_command(proto2, "game_info")
    await read_until_command(proto3, "game_info")

    await proto1.send_message({
        "command": "game_host",
        "title": "Friends Only",
        "mod": "faf",
        "visibility": "friends"
    })

    # The host and his friend should see the game
    msg = await read_until_command(proto1, "game_info")
    msg2 = await read_until_command(proto2, "game_info")

    assert msg == msg2
    assert msg["featured_mod"] == "faf"
    assert msg["title"] == "Friends Only"
    assert msg["visibility"] == "friends"

    # However, the other person should not see the game
    with pytest.raises(asyncio.TimeoutError):
        await read_until_command(proto3, "game_info", timeout=1)


@pytest.mark.parametrize("user", [
    ("test", "test_password"),
    ("ban_revoked", "ban_revoked"),
    ("ban_expired", "ban_expired"),
    ("No_UID", "his_pw"),
    ("steam_id", "steam_id")
])
async def test_game_host_authenticated(lobby_server, user):
    _, _, proto = await connect_and_sign_in(user, lobby_server)
    await read_until_command(proto, 'game_info')

    await proto.send_message({
        'command': 'game_host',
        'title': 'My Game',
        'mod': 'faf',
        'visibility': 'public',
    })

    msg = await read_until_command(proto, 'game_launch')

    assert msg['mod'] == 'faf'
    assert 'args' in msg
    assert isinstance(msg['uid'], int)


@fast_forward(5)
async def test_host_missing_fields(event_loop, lobby_server, player_service):
    player_id, session, proto = await connect_and_sign_in(
        ('test', 'test_password'),
        lobby_server
    )

    await read_until_command(proto, 'game_info')

    await proto.send_message({
        'command': 'game_host',
        'mod': '',
        'visibility': 'public',
        'title': ''
    })

    msg = await read_until_command(proto, 'game_info')

    assert msg['title'] == 'test&#x27;s game'
    assert msg['mapname'] == 'scmp_007'
    assert msg['map_file_path'] == 'maps/scmp_007.zip'
    assert msg['featured_mod'] == 'faf'


@pytest.mark.parametrize("command", ["game_host", "game_join"])
async def test_server_ban_prevents_hosting(lobby_server, database, command):
    """
    Players who are banned while they are online, should immediately be
    prevented from joining or hosting games until their ban expires.
    """
    player_id, _, proto = await connect_and_sign_in(
        ('banme', 'banme'), lobby_server
    )
    # User successfully logs in
    await read_until_command(proto, 'game_info')

    async with database.acquire() as conn:
        await conn.execute(
            ban.insert().values(
                player_id=player_id,
                author_id=player_id,
                reason="Test live ban",
                expires_at=None,
                level='GLOBAL'
            )
        )

    await proto.send_message({"command": command})

    msg = await proto.read_message()
    assert msg == {
        'command': 'notice',
        'style': 'error',
        'text': 'You are banned from FAF forever.\n Reason :\n Test live ban'
    }
    proto.close()


@fast_forward(5)
async def test_coop_list(lobby_server):
    _, _, proto = await connect_and_sign_in(
        ('test', 'test_password'),
        lobby_server
    )

    await read_until_command(proto, 'game_info')

    await proto.send_message({"command": "coop_list"})

    msg = await read_until_command(proto, "coop_info")
    assert "name" in msg
    assert "description" in msg
    assert "filename" in msg


async def test_ice_servers_empty(lobby_server):
    _, _, proto = await connect_and_sign_in(
        ('test', 'test_password'),
        lobby_server
    )

    await read_until_command(proto, 'game_info')

    await proto.send_message({"command": "ice_servers"})

    msg = await read_until_command(proto, "ice_servers")

    # By default the server config should not have any ice servers
    assert msg == {
        'command': 'ice_servers',
        'ice_servers': [],
        'ttl': 86400
    }


async def get_player_selected_avatars(conn, player_id):
    return await conn.execute(
        select([avatars.c.id, avatars_list.c.url])
        .select_from(avatars_list.join(avatars))
        .where(
            and_(
                avatars.c.idUser == player_id,
                avatars.c.selected == 1,
            )
        )
    )


@fast_forward(30)
async def test_avatar_select(lobby_server, database):
    # This user has multiple avatars in the test data
    player_id, _, proto = await connect_and_sign_in(
        ('player_service1', 'player_service1'),
        lobby_server
    )
    await read_until_command(proto, 'game_info')
    # Skip any latent player broadcasts
    with contextlib.suppress(asyncio.TimeoutError):
        await read_until_command(proto, 'player_info', timeout=5)

    await proto.send_message({
        "command": "avatar", "action": "list_avatar"
    })

    msg = await read_until_command(proto, "avatar")
    avatar_list = msg["avatarlist"]

    for avatar in avatar_list:
        await proto.send_message({
            "command": "avatar",
            "action": "select",
            "avatar": avatar["url"]
        })
        msg = await read_until_command(proto, "player_info")
        assert msg["players"][0]["avatar"] == avatar

    async with database.acquire() as conn:
        result = await get_player_selected_avatars(conn, player_id)
        assert result.rowcount == 1
        row = await result.fetchone()
        assert row[avatars_list.c.url] == avatar["url"]

    await proto.send_message({
        "command": "avatar",
        "action": "select",
        "avatar": "BOGUS!"
    })
    with pytest.raises(asyncio.TimeoutError):
        await read_until_command(proto, "player_info", timeout=10)

    async with database.acquire() as conn:
        result = await get_player_selected_avatars(conn, player_id)
        assert result.rowcount == 1
        row = await result.fetchone()
        assert row[avatars_list.c.url] == avatar["url"]


@fast_forward(30)
async def test_avatar_select_not_owned(lobby_server, database):
    # This user has no avatars
    player_id, _, proto = await connect_and_sign_in(
        ('test', 'test_password'),
        lobby_server
    )
    await read_until_command(proto, 'game_info')
    # Skip any latent player broadcasts
    with contextlib.suppress(asyncio.TimeoutError):
        await read_until_command(proto, 'player_info', timeout=5)

    await proto.send_message({
        "command": "avatar",
        "action": "select",
        "avatar": "http://content.faforever.com/faf/avatars/UEF.png"
    })
    with pytest.raises(asyncio.TimeoutError):
        await read_until_command(proto, "player_info", timeout=10)

    async with database.acquire() as conn:
        result = await get_player_selected_avatars(conn, player_id)
        assert result.rowcount == 0
