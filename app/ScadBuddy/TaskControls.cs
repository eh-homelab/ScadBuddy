using System.Globalization;
using System.Text.Json.Nodes;

namespace ScadBuddy;

/// <summary>
/// One native control per <see cref="ControlKind"/> (#8574, spec §6.4). Each shows its label,
/// its description and a problem under it, and gives its value as <see cref="TaskForm.Answer"/>
/// takes it: text, a bool, or null.
/// </summary>
abstract class TaskControl : ContentView
{
	readonly Label problem = TaskTheme.Problem();
	protected readonly VerticalStackLayout Body = new() { Spacing = 8 };

	protected TaskControl(FieldSpec field)
	{
		Field = field;
		var stack = new VerticalStackLayout { Spacing = 6 };
		if (field.Kind != ControlKind.Confirm)
			stack.Add(TaskTheme.FieldLabel(field.Required ? field.Label : field.Label + " (optional)"));
		if (field.Description is { Length: > 0 } description) stack.Add(TaskTheme.Small(description));
		stack.Add(Body);
		stack.Add(problem);
		Content = stack;
	}

	public FieldSpec Field { get; }

	public abstract JsonNode? Value { get; }

	/// <summary>A problem the control found itself (a code that does not fit), shown before the form's own check.</summary>
	public virtual string? LocalProblem => null;

	public void Show(string? text)
	{
		problem.Text = text;
		problem.IsVisible = !string.IsNullOrEmpty(text);
	}

	/// <summary>The control for a field. <paramref name="page"/> hosts the tag read's screen.</summary>
	public static TaskControl For(FieldSpec field, string taskId, Page page) => field.Kind switch
	{
		ControlKind.Text => new TextControl(field, Keyboard.Text),
		ControlKind.Email => new TextControl(field, Keyboard.Email),
		ControlKind.Date => new DateControl(field, withTime: false),
		ControlKind.DateTime => new DateControl(field, withTime: true),
		ControlKind.Choice => new ChoiceControl(field),
		ControlKind.Switch => new SwitchControl(field),
		ControlKind.Number => new NumberControl(field, null),
		ControlKind.Weight => new NumberControl(field, "g"),
		ControlKind.Lines => new LinesControl(field),
		ControlKind.Confirm => new ConfirmControl(field),
		ControlKind.Photo => new PhotoControl(field, taskId),
		ControlKind.Scan or ControlKind.Entity => new ScanControl(field, taskId, page),
		_ => throw new ArgumentOutOfRangeException(nameof(field)),
	};

	protected static string? DefaultText(FieldSpec field) => field.Default is null ? null : TaskForm.ChoiceText(field.Default);
}

sealed class TextControl : TaskControl
{
	readonly Entry entry;

	public TextControl(FieldSpec field, Keyboard keyboard) : base(field)
	{
		entry = TaskTheme.Entry(keyboard);
		entry.Text = DefaultText(field);
		Body.Add(entry);
	}

	public override JsonNode? Value => entry.Text;
}

sealed class DateControl : TaskControl
{
	readonly DatePicker date = new() { MinimumHeightRequest = TaskTheme.Touch, FontSize = 18 };
	readonly TimePicker time = new() { MinimumHeightRequest = TaskTheme.Touch, FontSize = 18 };
	readonly Switch given = new() { IsToggled = true };
	readonly bool withTime;

	public DateControl(FieldSpec field, bool withTime) : base(field)
	{
		this.withTime = withTime;
		var start = DateTime.Now;
		if (DefaultText(field) is { } text && DateTimeOffset.TryParse(text, CultureInfo.InvariantCulture, DateTimeStyles.AssumeLocal, out var at))
			start = at.LocalDateTime;
		date.Date = start.Date;
		time.Time = start.TimeOfDay;
		var row = new HorizontalStackLayout { Spacing = 16, Children = { date } };
		if (withTime) row.Add(time);
		if (!field.Required)
		{
			// An optional date can be left out.
			given.IsToggled = field.Default is not null;
			row.IsVisible = given.IsToggled;
			given.Toggled += (_, e) => row.IsVisible = e.Value;
			Body.Add(new HorizontalStackLayout { Spacing = 8, Children = { given, TaskTheme.Body(withTime ? "Give a time" : "Give a date", 15) } });
		}
		Body.Add(row);
	}

	public override JsonNode? Value
	{
		get
		{
			if (!given.IsToggled || date.Date is not { } day) return null;
			if (!withTime) return TaskForm.DateText(day);
			var local = day.Date + (time.Time ?? TimeSpan.Zero);
			return TaskForm.DateTimeText(local, TimeZoneInfo.Local.GetUtcOffset(local));
		}
	}
}

sealed class ChoiceControl : TaskControl
{
	string? chosen;
	readonly Picker? picker;

	public ChoiceControl(FieldSpec field) : base(field)
	{
		var options = field.Options ?? [];
		chosen = DefaultText(field);
		if (options.Length <= 5)
		{
			// Up to five: one tap each, as the web's segmented buttons.
			var buttons = new List<Button>();
			var row = new FlexLayout { Wrap = Microsoft.Maui.Layouts.FlexWrap.Wrap };
			foreach (var option in options)
			{
				var button = TaskTheme.Quiet(option);
				button.Margin = new Thickness(0, 0, 8, 8);
				button.Padding = new Thickness(18, 0);
				button.Clicked += (_, _) =>
				{
					// A second tap on an optional choice clears it.
					chosen = chosen == option && !field.Required ? null : option;
					foreach (var b in buttons) TaskTheme.Chosen(b, b.Text == chosen);
				};
				TaskTheme.Chosen(button, option == chosen);
				buttons.Add(button);
				row.Add(button);
			}
			Body.Add(row);
		}
		else
		{
			picker = new Picker { ItemsSource = options, Title = "Choose", FontSize = 18, MinimumHeightRequest = TaskTheme.Touch };
			if (chosen is not null) picker.SelectedItem = chosen;
			Body.Add(picker);
		}
	}

	public override JsonNode? Value => picker is null ? chosen : picker.SelectedItem as string;
}

sealed class SwitchControl : TaskControl
{
	readonly Switch toggle = new();

	public SwitchControl(FieldSpec field) : base(field)
	{
		toggle.IsToggled = field.Default is JsonValue v && v.TryGetValue<bool>(out var on) && on;
		Body.Add(toggle);
	}

	public override JsonNode? Value => toggle.IsToggled;
}

sealed class NumberControl : TaskControl
{
	readonly Entry entry;

	public NumberControl(FieldSpec field, string? unit) : base(field)
	{
		entry = TaskTheme.Entry(Keyboard.Numeric, unit is null ? null : "grams");
		entry.Text = DefaultText(field);
		entry.HorizontalOptions = LayoutOptions.Fill;
		var row = new Grid { ColumnDefinitions = [new(GridLength.Star), new(GridLength.Auto), new(GridLength.Auto)], ColumnSpacing = 8 };
		row.Add(entry, 0);
		if (unit is not null)
		{
			row.Add(TaskTheme.Body(unit, 18).Also(l => l.VerticalOptions = LayoutOptions.Center), 1);
		}
		else if (field.Type == "integer")
		{
			// A whole number gets a stepper, kept in its bounds.
			var less = TaskTheme.Quiet("−");
			var more = TaskTheme.Quiet("+");
			less.WidthRequest = more.WidthRequest = TaskTheme.Touch;
			less.Clicked += (_, _) => Step(-1);
			more.Clicked += (_, _) => Step(1);
			row.Add(less, 1);
			row.Add(more, 2);
		}
		Body.Add(row);
		if (field.Min is not null || field.Max is not null)
			Body.Add(TaskTheme.Small(field.Min is { } min && field.Max is { } max ? $"From {Show(min)} to {Show(max)}" : field.Min is { } lo ? $"At least {Show(lo)}" : $"At most {Show(field.Max!.Value)}"));
	}

	static string Show(double n) => n.ToString(CultureInfo.InvariantCulture);

	void Step(int by)
	{
		var now = long.TryParse(entry.Text, NumberStyles.Integer, CultureInfo.InvariantCulture, out var n) ? n : (long)(Field.Min ?? 0) - (by > 0 ? 1 : -1);
		var next = (double)(now + by);
		if (Field.Min is { } min && next < min) next = min;
		if (Field.Max is { } max && next > max) next = max;
		entry.Text = Show(next);
	}

	public override JsonNode? Value => entry.Text;
}

sealed class LinesControl : TaskControl
{
	readonly Editor editor = new() { AutoSize = EditorAutoSizeOption.TextChanges, MinimumHeightRequest = 96, FontSize = 17 };

	public LinesControl(FieldSpec field) : base(field)
	{
		if (field.Default is JsonArray items) editor.Text = string.Join("\n", items.Select(TaskForm.ChoiceText));
		Body.Add(editor);
		Body.Add(TaskTheme.Small("One per line."));
	}

	public override JsonNode? Value => editor.Text;
}

/// <summary>A confirm has no input: its answer is true when the person sends. Its label goes on the send button when it is the only field.</summary>
sealed class ConfirmControl : TaskControl
{
	public ConfirmControl(FieldSpec field) : base(field) => Body.Add(TaskTheme.Body(field.Label, 17));

	public override JsonNode? Value => true;
}

sealed class PhotoControl : TaskControl
{
	readonly string taskId;
	readonly Image preview = new() { HeightRequest = 180, Aspect = Aspect.AspectFit, IsVisible = false };
	readonly Label state = TaskTheme.Small("");
	string? attachment;

	public PhotoControl(FieldSpec field, string taskId) : base(field)
	{
		this.taskId = taskId;
		var take = TaskTheme.Quiet("Take a photo");
		take.Clicked += async (_, _) =>
		{
			take.IsEnabled = false;
			try { await Take(take); }
			finally { take.IsEnabled = true; }
		};
		Body.Add(take);
		Body.Add(preview);
		Body.Add(state);
	}

	async Task Take(Button take)
	{
		Show(null);
		(byte[] Bytes, string MediaType)? photo;
		try { photo = await TaskReads.TakePhoto(Field.Label); }
		catch (InvalidOperationException error) { Show(error.Message); return; }
		if (photo is not { } taken) return;
		state.Text = "Sending the photo…";
		try
		{
			attachment = await TaskInbox.Instance.UploadPhoto(taskId, taken.Bytes, taken.MediaType);
			preview.Source = ImageSource.FromStream(() => new MemoryStream(taken.Bytes));
			preview.IsVisible = true;
			state.Text = "Photo added.";
			take.Text = "Take another photo";
		}
		catch (Exception error)
		{
			state.Text = "";
			Show(error is InvalidOperationException ? error.Message : "The photo was not sent. Check the connection and take it again.");
		}
	}

	public override JsonNode? Value => attachment;
}

/// <summary>
/// A scan or entity field, as the web's ScanField: a typed code, "Hold a tag" (the app's NFC
/// reader) or "Scan a code" (a photo), the last two looked up by core (<c>task-scan</c>). The
/// code and what it names show before the answer is sent; a code that does not fit is refused.
/// </summary>
sealed class ScanControl : TaskControl
{
	const string Misfit = "That is not one of the codes this question asks for.";
	readonly string taskId;
	readonly Page page;
	readonly Entry code;
	readonly ContentView found = new();
	string? value;

	// What a typed code must be: the scan field's expects, or an entity field's kind.
	JsonObject? Expects => Field.Expects ?? (Field.EntityKind is { } kind ? new JsonObject { ["kinds"] = new JsonArray(kind) } : null);

	public ScanControl(FieldSpec field, string taskId, Page page) : base(field)
	{
		this.taskId = taskId;
		this.page = page;
		code = TaskTheme.Entry(Keyboard.Create(KeyboardFlags.CapitalizeCharacter), "Type a code, such as S-12");
		code.FontFamily = "monospace";
		code.TextChanged += (_, _) =>
		{
			// A typed code is the answer only once it is whole (Enter, or leaving the entry).
			value = null;
			found.Content = null;
			Show(null);
		};
		code.Completed += (_, _) => Typed();
		code.Unfocused += (_, _) => Typed();
		var tag = TaskTheme.Quiet("Hold a tag");
		tag.Clicked += async (_, _) => await Read(tag, fromTag: true);
		var camera = TaskTheme.Quiet("Scan a code");
		camera.Clicked += async (_, _) => await Read(camera, fromTag: false);
		var buttons = new Grid { ColumnDefinitions = [new(GridLength.Star), new(GridLength.Star)], ColumnSpacing = 8 };
		buttons.Add(tag, 0);
		buttons.Add(camera, 1);
		Body.Add(buttons);
		Body.Add(code);
		Body.Add(found);
	}

	void Typed()
	{
		var text = code.Text?.Trim() ?? "";
		if (text.Length == 0 || value is not null) return;
		var canonical = TaskForm.CanonicalCode(text);
		if (!TaskForm.CodeFits(canonical, Expects))
		{
			Show(Misfit);
			return;
		}
		// The server checks what the code names when the answer is sent.
		value = canonical;
	}

	async Task Read(Button button, bool fromTag)
	{
		button.IsEnabled = false;
		Show(null);
		try
		{
			ScanResult result;
			if (fromTag)
			{
				var dump = await TaskReads.ReadTag(page.Navigation);
				if (dump is null) return;
				result = await TaskInbox.Instance.Scan(taskId, Field.Name, dump, null, null);
			}
			else
			{
				if (await TaskReads.TakePhoto("Photo of the code") is not { } photo) return;
				result = await TaskInbox.Instance.Scan(taskId, Field.Name, null, photo.Bytes, photo.MediaType);
			}
			if (result.Code is null)
			{
				Show(fromTag ? "That tag holds no ScadBuddy code." : "No code was found in the photo. Take it again, closer.");
				return;
			}
			if (!result.Fits)
			{
				Show(Misfit);
				return;
			}
			code.Text = result.Code;
			value = result.Code;
			found.Content = TaskTheme.Card(new VerticalStackLayout
			{
				Spacing = 2,
				Children = { TaskTheme.Body(result.Name ?? result.Code, 17), TaskTheme.Small(result.Code) },
			}, 12);
		}
		catch (Exception error)
		{
			Show(error is InvalidOperationException ? error.Message : "The read was not looked up. Check the connection and read it again.");
		}
		finally { button.IsEnabled = true; }
	}

	public override JsonNode? Value
	{
		get
		{
			Typed();
			return value;
		}
	}

	public override string? LocalProblem
	{
		get
		{
			var text = code.Text?.Trim() ?? "";
			return value is null && text.Length > 0 && !TaskForm.CodeFits(text, Expects) ? Misfit : null;
		}
	}
}

static class ViewExtensions
{
	public static T Also<T>(this T view, Action<T> change)
	{
		change(view);
		return view;
	}
}
