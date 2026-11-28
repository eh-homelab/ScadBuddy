using System.Text.RegularExpressions;

namespace ScadBuddy;

/// <summary>
/// A task's context blocks as native views (#8574, spec §6.4; the web's ContextBlocks.tsx): an
/// entity card with the thing's colour, text, an image, a table, a checklist (ticks stay on the
/// phone), an expected weight, and any other item as names and values. All of it is text and
/// views, never markup.
/// </summary>
static partial class ContextViews
{
	public static View For(ContextBlock block, string taskId) => block switch
	{
		EntityBlock entity => Entity(entity),
		TextBlock text => Text(text.Text),
		ImageBlock image => Image(image, taskId),
		TableBlock table => Table(table),
		ChecklistBlock list => Checklist(list),
		ExpectBlock expect => TaskTheme.Card(TaskTheme.Body(expect.Text, 22).Also(l => l.FontFamily = "OpenSansSemibold")),
		PairsBlock pairs => Pairs(pairs),
		_ => new ContentView(),
	};

	static View Entity(EntityBlock card)
	{
		var grid = new Grid { ColumnDefinitions = [new(GridLength.Auto), new(GridLength.Star)], ColumnSpacing = 14 };
		var text = new VerticalStackLayout { Spacing = 2, VerticalOptions = LayoutOptions.Center };
		text.Add(TaskTheme.Body(card.Title, 18).Also(l => l.FontFamily = "OpenSansSemibold"));
		var line = string.Join("  ", new[] { card.Code, card.Subtitle }.Where(s => !string.IsNullOrEmpty(s)));
		if (line.Length > 0) text.Add(TaskTheme.Small(line));
		if (TaskTheme.Swatch(card.Rgba, 44) is { } swatch) grid.Add(swatch, 0);
		grid.Add(text, 1);
		return TaskTheme.Card(grid);
	}

	[GeneratedRegex(@"\*\*(.+?)\*\*|__(.+?)__|\*(.+?)\*|_(.+?)_|`(.+?)`")]
	private static partial Regex Emphasis();

	/// <summary>Markdown as plain paragraphs, lists and emphasis (spec §6.4): no links, no markup.</summary>
	static View Text(string markdown)
	{
		var stack = new VerticalStackLayout { Spacing = 8 };
		foreach (var paragraph in Regex.Split(markdown.Replace("\r", ""), "\n\\s*\n"))
		{
			var lines = paragraph.Split('\n').Select(l => Regex.Replace(l.TrimEnd(), @"^\s*([-*+]|\d+\.)\s+", m => m.Value.Trim() is "-" or "*" or "+" ? "•  " : m.Value.TrimStart()));
			var label = new Label { FontSize = 16, LineBreakMode = LineBreakMode.WordWrap, FormattedText = Spans(string.Join("\n", lines).TrimStart('#', ' ')) };
			stack.Add(label);
		}
		return stack;
	}

	static FormattedString Spans(string text)
	{
		var formatted = new FormattedString();
		var at = 0;
		foreach (Match m in Emphasis().Matches(text))
		{
			if (m.Index > at) formatted.Spans.Add(Plain(text[at..m.Index]));
			var span = Plain(m.Groups.Cast<Group>().Skip(1).First(g => g.Success).Value);
			if (m.Value.StartsWith("**") || m.Value.StartsWith("__")) span.FontAttributes = FontAttributes.Bold;
			else if (m.Value.StartsWith('`')) span.FontFamily = "monospace";
			else span.FontAttributes = FontAttributes.Italic;
			formatted.Spans.Add(span);
			at = m.Index + m.Length;
		}
		if (at < text.Length) formatted.Spans.Add(Plain(text[at..]));
		return formatted;
	}

	static Span Plain(string text)
	{
		var span = new Span { Text = text };
		span.SetAppThemeColor(Span.TextColorProperty, Color.FromArgb("#0A0A0A"), Color.FromArgb("#FAFAFA"));
		return span;
	}

	static View Image(ImageBlock block, string taskId)
	{
		var image = new Image { HeightRequest = 220, Aspect = Aspect.AspectFit };
		var note = TaskTheme.Small("Loading the picture…");
		_ = Load();
		return new VerticalStackLayout { Spacing = 4, Children = { image, note } };

		async Task Load()
		{
			var bytes = await TaskInbox.Instance.Image(taskId, block.AttachmentId);
			await MainThread.InvokeOnMainThreadAsync(() =>
			{
				if (bytes is null)
				{
					note.Text = "The picture could not be loaded.";
					return;
				}
				image.Source = ImageSource.FromStream(() => new MemoryStream(bytes));
				note.IsVisible = false;
			});
		}
	}

	static View Table(TableBlock table)
	{
		var grid = new Grid { ColumnSpacing = 16, RowSpacing = 6 };
		foreach (var _ in table.Columns) grid.ColumnDefinitions.Add(new ColumnDefinition(GridLength.Auto));
		for (var r = 0; r <= table.Rows.Count; r++) grid.RowDefinitions.Add(new RowDefinition(GridLength.Auto));
		for (var c = 0; c < table.Columns.Length; c++)
			grid.Add(TaskTheme.Small(table.Columns[c]).Also(l => l.FontFamily = "OpenSansSemibold"), c, 0);
		for (var r = 0; r < table.Rows.Count; r++)
			for (var c = 0; c < table.Rows[r].Length; c++)
				grid.Add(TaskTheme.Body(table.Rows[r][c], 15), c, r + 1);
		// A wide table scrolls sideways; the page does not.
		return TaskTheme.Card(new ScrollView { Orientation = ScrollOrientation.Horizontal, Content = grid });
	}

	static View Checklist(ChecklistBlock list)
	{
		var stack = new VerticalStackLayout { Spacing = 2 };
		foreach (var item in list.Items)
		{
			var box = new CheckBox { VerticalOptions = LayoutOptions.Center };
			var label = TaskTheme.Body(item);
			label.VerticalOptions = LayoutOptions.Center;
			var row = new Grid { ColumnDefinitions = [new(GridLength.Auto), new(GridLength.Star)], MinimumHeightRequest = TaskTheme.Touch };
			row.Add(box, 0);
			row.Add(label, 1);
			// The whole row ticks, not only the box.
			row.GestureRecognizers.Add(new TapGestureRecognizer { Command = new Command(() => box.IsChecked = !box.IsChecked) });
			stack.Add(row);
		}
		return stack;
	}

	static View Pairs(PairsBlock pairs)
	{
		var grid = new Grid { ColumnDefinitions = [new(new GridLength(2, GridUnitType.Star)), new(new GridLength(3, GridUnitType.Star))], ColumnSpacing = 12, RowSpacing = 6 };
		for (var i = 0; i < pairs.Pairs.Count; i++)
		{
			grid.RowDefinitions.Add(new RowDefinition(GridLength.Auto));
			grid.Add(TaskTheme.Small(pairs.Pairs[i].Name), 0, i);
			grid.Add(TaskTheme.Body(pairs.Pairs[i].Value, 15), 1, i);
		}
		return TaskTheme.Card(grid);
	}
}
