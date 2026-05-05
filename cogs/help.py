"""cogs/help.py — Paginated /help command for Enigma."""

import discord
from discord import app_commands
from discord.ext import commands

COLOUR = discord.Colour.from_str("#5865F2")

# Each entry: (page title, [top-level command names shown on that page])
PAGES = [
    ("⚙️ Admin & 🏆 Leaderboards",  ["game", "leaderboard"]),
    ("🧠 Classic Trivia",            ["trivia", "categoryking", "ckanswer", "stump", "enigma", "chain", "ciq"]),
    ("🔍 Specialist Knowledge",      ["niche", "deepcut", "dcanswer", "tot", "source"]),
    ("📝 Wordplay",                  ["anagram", "defduel", "synonym", "etymology", "portmanteau", "bee", "acronym", "fw", "cwc"]),
    ("🧩 Logic & Reasoning",         ["ll", "pb", "link", "cs", "inf", "fermi", "price", "prob", "tl"]),
    ("🖼️ Visual & Media",            ["blurred", "macro", "map", "silhouette", "colour", "logo", "ol", "char"]),
    ("👥 Member-Generated Content",  ["qc", "fd", "exam"]),
]


def _cmd_lines(cmd: discord.app_commands.AppCommand) -> list[str]:
    """Return formatted `</name:id> — description` lines, expanding subcommands."""
    subs = [o for o in (cmd.options or []) if o.type.value == 1]  # type 1 = subcommand
    if not subs:
        return [f"</{cmd.name}:{cmd.id}> — {cmd.description}"]
    return [f"</{cmd.name} {sub.name}:{cmd.id}> — {sub.description}" for sub in subs]


def _build_embeds(fetched: list[discord.app_commands.AppCommand]) -> list[discord.Embed]:
    cmd_map = {c.name: c for c in fetched}
    total = len(PAGES)
    embeds = []
    for idx, (title, names) in enumerate(PAGES, 1):
        lines: list[str] = []
        for name in names:
            cmd = cmd_map.get(name)
            if cmd:
                lines.extend(_cmd_lines(cmd))
        embed = discord.Embed(
            title=f"📖 Enigma Help — {title}",
            description="\n".join(lines) or "*No commands found.*",
            colour=COLOUR,
        )
        embed.set_footer(text=f"Page {idx} of {total} • use ◀ ▶ to navigate")
        embeds.append(embed)
    return embeds


class HelpView(discord.ui.View):
    def __init__(self, embeds: list[discord.Embed]):
        super().__init__(timeout=120)
        self.embeds = embeds
        self.page = 0
        self.message: discord.Message | None = None
        self._sync_buttons()

    def _sync_buttons(self):
        self.prev_btn.disabled = self.page == 0
        self.next_btn.disabled = self.page == len(self.embeds) - 1

    @discord.ui.button(label="◀ Prev", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page -= 1
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.embeds[self.page], view=self)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page += 1
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.embeds[self.page], view=self)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True  # type: ignore[union-attr]
        if self.message:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass


class HelpCog(commands.Cog, name="Help"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="help", description="Browse all Enigma commands with clickable links.")
    async def help_cmd(self, interaction: discord.Interaction):
        await interaction.response.defer()
        fetched = await interaction.client.tree.fetch_commands()
        embeds = _build_embeds(fetched)
        view = HelpView(embeds)
        view.message = await interaction.followup.send(embed=embeds[0], view=view)


async def setup(bot: commands.Bot):
    await bot.add_cog(HelpCog(bot))
