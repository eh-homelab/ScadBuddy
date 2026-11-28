using System.Globalization;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace ScadBuddy;

/// <summary>
/// The pure part of the native task screens (#8574, spec
/// docs/superpowers/specs/2026-11-28-native-task-screens-design.md §6.4): a task as core's
/// device task routes send it, the control each schema field takes, the answer the controls
/// give, and the context blocks. It uses no MAUI or Android type, so ScadBuddy.Tests compiles
/// this file on its own (net10.0). The rules follow the web form (#8181,
/// frontend/src/lib/flowInputs.ts, components/flows/ContextBlocks.tsx).
/// </summary>
public static class TaskForm
{
	/// <summary>The string formats a text entry draws: none, email, date and date-time.</summary>
	static readonly string?[] Formats = [null, "email", "date", "date-time"];
	static readonly string[] Scalars = ["string", "number", "integer", "boolean"];
	static readonly string[] States = ["open", "answered", "cancelled", "expired"];

	/// <summary>A task from core's JSON, or null when the object is not one.</summary>
	public static DeviceTask? TaskOf(JsonObject o)
	{
		try
		{
			var id = Str(o, "id");
			var run = Str(o, "run_id");
			var title = Str(o, "title");
			var state = Str(o, "state");
			if (string.IsNullOrEmpty(id) || run is null || string.IsNullOrEmpty(title) || state is null || !States.Contains(state)) return null;
			if (o["schema"] is not JsonObject schema) return null;
			var context = o["context"] as JsonArray ?? [];
			return new DeviceTask(
				id, run, title, (JsonObject)schema.DeepClone(), (JsonArray)context.DeepClone(),
				Str(o, "place"), Str(o, "ui"), state,
				o["native"] is JsonValue n && n.TryGetValue<bool>(out var native) && native,
				Str(o, "fallback_url") ?? "/station",
				(o["context_view"] as JsonArray)?.DeepClone() as JsonArray);
		}
		catch (InvalidOperationException)
		{
			return null;
		}
	}

	/// <summary>The tasks of <c>GET /devices/tasks</c>'s answer; a task of another shape is left out. Null when the text is no list.</summary>
	public static List<DeviceTask>? ListOf(string text)
	{
		try
		{
			if (JsonNode.Parse(text) is not JsonObject o || o["tasks"] is not JsonArray tasks) return null;
			return tasks.OfType<JsonObject>().Select(TaskOf).OfType<DeviceTask>().ToList();
		}
		catch (JsonException) { return null; }
	}

	/// <summary>One task's answer (<c>GET /devices/task</c>), or null.</summary>
	public static DeviceTask? OneOf(string text)
	{
		try { return JsonNode.Parse(text) is JsonObject o ? TaskOf(o) : null; }
		catch (JsonException) { return null; }
	}

	/// <summary>
	/// The controls of a task's schema, in its order, or null when one field is not one the app
	/// draws (then the phone opens the web screen, never a half form). The server says so first
	/// (<c>native</c>); this is the second guard.
	/// </summary>
	public static IReadOnlyList<FieldSpec>? Fields(JsonObject schema)
	{
		try
		{
			if (Str(schema, "type") is { } type && type != "object") return null;
			var properties = schema["properties"] as JsonObject ?? [];
			var required = (schema["required"] as JsonArray ?? []).Select(r => r is JsonValue v && v.TryGetValue<string>(out var s) ? s : null).ToHashSet();
			var fields = new List<FieldSpec>();
			foreach (var (name, node) in properties)
			{
				if (node is not JsonObject prop) return null;
				var field = FieldOf(name, prop, required.Contains(name));
				if (field is null) return null;
				fields.Add(field);
			}
			return fields;
		}
		catch (InvalidOperationException) { return null; }
	}

	static FieldSpec? FieldOf(string name, JsonObject prop, bool required)
	{
		var type = Str(prop, "type");
		var label = Str(prop, "title") is { Length: > 0 } title ? title : name.Replace('_', ' ');
		var spec = new FieldSpec(name, label, ControlKind.Text, required, prop["default"]?.DeepClone(), null, Num(prop, "minimum"), Num(prop, "maximum"), null, null)
		{
			Type = type ?? "",
			Description = Str(prop, "description"),
		};
		if (prop.ContainsKey("x-sb-widget"))
		{
			var widget = Str(prop, "x-sb-widget");
			return widget switch
			{
				"confirm" when type == "boolean" => spec with { Kind = ControlKind.Confirm },
				"scan" when type == "string" => spec with { Kind = ControlKind.Scan, Expects = (prop["expects"] as JsonObject)?.DeepClone() as JsonObject },
				"photo" when type == "string" => spec with { Kind = ControlKind.Photo },
				"entity" when type == "string" && Str(prop, "kind") is "spool" or "location" => spec with { Kind = ControlKind.Entity, EntityKind = Str(prop, "kind") },
				"weight" when type is "number" or "integer" => spec with { Kind = ControlKind.Weight },
				// file, an entity of a library file, and any widget the app does not know.
				_ => null,
			};
		}
		if (prop["enum"] is JsonArray choices)
		{
			if (type is not ("string" or "number" or "integer") || choices.Count == 0 || choices.Any(c => c is not JsonValue)) return null;
			return spec with { Kind = ControlKind.Choice, Enum = (JsonArray)choices.DeepClone() };
		}
		switch (type)
		{
			case "string":
				var format = Str(prop, "format");
				if (!Formats.Contains(format)) return null;
				return spec with { Kind = format switch { "email" => ControlKind.Email, "date" => ControlKind.Date, "date-time" => ControlKind.DateTime, _ => ControlKind.Text } };
			case "boolean":
				return spec with { Kind = ControlKind.Switch };
			case "number" or "integer":
				return spec with { Kind = ControlKind.Number };
			case "array":
				var items = prop["items"] as JsonObject;
				var itemType = items is null ? "string" : Str(items, "type");
				if (itemType is null || !Scalars.Contains(itemType)) return null;
				return spec with { Kind = ControlKind.Lines, ItemType = itemType };
			default:
				return null;
		}
	}

	/// <summary>
	/// The answer the controls give. A control's value is text (what was typed, picked or read),
	/// a bool (a switch), or null. An optional field left empty is left out; a confirm answers
	/// true; a number, a weight and a numeric choice answer a number; lines answer an array.
	/// </summary>
	public static JsonObject Answer(IReadOnlyList<FieldSpec> fields, IReadOnlyDictionary<string, JsonNode?> values)
	{
		var answer = new JsonObject();
		foreach (var field in fields)
		{
			if (field.Kind == ControlKind.Confirm)
			{
				answer[field.Name] = true;
				continue;
			}
			values.TryGetValue(field.Name, out var value);
			if (Typed(field, value) is { } typed) answer[field.Name] = typed;
		}
		return answer;
	}

	/// <summary>
	/// What the phone can tell before it sends: a required field left empty, a number that is
	/// not one, or out of its bounds. The server checks the answer again (its 422 is shown the same way).
	/// </summary>
	public static List<(string Field, string Message)> Check(IReadOnlyList<FieldSpec> fields, IReadOnlyDictionary<string, JsonNode?> values)
	{
		var problems = new List<(string, string)>();
		foreach (var field in fields)
		{
			if (field.Kind is ControlKind.Confirm or ControlKind.Switch) continue;
			values.TryGetValue(field.Name, out var value);
			var text = TextOf(value);
			if (string.IsNullOrWhiteSpace(text))
			{
				if (field.Required) problems.Add((field.Name, $"{field.Label} is needed."));
				continue;
			}
			if (field.Kind is not (ControlKind.Number or ControlKind.Weight)) continue;
			if (!double.TryParse(text.Trim(), NumberStyles.Float, CultureInfo.InvariantCulture, out var number))
			{
				problems.Add((field.Name, "Give a number."));
				continue;
			}
			if (field.Type == "integer" && number != Math.Floor(number))
			{
				problems.Add((field.Name, "Give a whole number."));
				continue;
			}
			if (field.Min is { } min && number < min) problems.Add((field.Name, $"Give at least {Show(min)}."));
			else if (field.Max is { } max && number > max) problems.Add((field.Name, $"Give at most {Show(max)}."));
		}
		return problems;
	}

	static JsonNode? Typed(FieldSpec field, JsonNode? value)
	{
		if (field.Kind == ControlKind.Switch)
			return value is JsonValue v && v.TryGetValue<bool>(out var on) ? on : field.Required ? false : null;
		var text = TextOf(value);
		if (string.IsNullOrWhiteSpace(text)) return null;
		switch (field.Kind)
		{
			case ControlKind.Number or ControlKind.Weight:
				return NumberOf(text.Trim(), field.Type == "integer");
			case ControlKind.Choice:
				return field.Enum!.FirstOrDefault(c => ChoiceText(c) == text)?.DeepClone();
			case ControlKind.Lines:
				var items = new JsonArray();
				foreach (var line in text.Split('\n').Select(l => l.Trim()).Where(l => l.Length > 0))
				{
					JsonNode? item = field.ItemType switch
					{
						"number" or "integer" => NumberOf(line, field.ItemType == "integer"),
						"boolean" => line.Equals("true", StringComparison.OrdinalIgnoreCase) ? true : line.Equals("false", StringComparison.OrdinalIgnoreCase) ? false : line,
						_ => line,
					};
					// A line that is not of the item type goes as text: the server says what is wrong with it.
					items.Add(item ?? line);
				}
				return items.Count == 0 ? null : items;
			default:
				return field.Kind is ControlKind.Text ? text : text.Trim();
		}
	}

	static JsonNode? NumberOf(string text, bool integer)
	{
		if (integer && long.TryParse(text, NumberStyles.Integer, CultureInfo.InvariantCulture, out var whole)) return whole;
		if (double.TryParse(text, NumberStyles.Float, CultureInfo.InvariantCulture, out var number)) return number;
		return text;
	}

	static string? TextOf(JsonNode? value) => value is JsonValue v && v.TryGetValue<string>(out var s) ? s : value?.ToJsonString();

	/// <summary>A choice's text, as its button shows it.</summary>
	public static string ChoiceText(JsonNode? choice) =>
		choice is JsonValue v && v.TryGetValue<string>(out var s) ? s : choice?.ToJsonString() ?? "";

	static string Show(double number) => number.ToString(CultureInfo.InvariantCulture);

	/// <summary>A date-time answer: the local time with its offset, as RFC 3339.</summary>
	public static string DateTimeText(DateTime local, TimeSpan offset) =>
		new DateTimeOffset(DateTime.SpecifyKind(local, DateTimeKind.Unspecified), offset).ToString("yyyy-MM-dd'T'HH:mm:sszzz", CultureInfo.InvariantCulture);

	/// <summary>A date answer: the day only.</summary>
	public static string DateText(DateTime day) => day.ToString("yyyy-MM-dd", CultureInfo.InvariantCulture);

	/// <summary>
	/// The context as the screen draws it: an item with one key of text, entity, image, table,
	/// checklist or expect is that block, any other is a list of names and values (as the web).
	/// An entity block takes its card from <c>context_view</c>, at the same index.
	/// </summary>
	public static List<ContextBlock> Blocks(DeviceTask task)
	{
		var blocks = new List<ContextBlock>();
		for (var i = 0; i < task.Context.Count; i++)
		{
			if (task.Context[i] is not JsonObject item || item.Count == 0) continue;
			var view = task.ContextView is { } views && i < views.Count ? views[i] as JsonObject : null;
			blocks.Add(BlockOf(item, view) ?? Pairs(item));
		}
		return blocks;
	}

	static ContextBlock? BlockOf(JsonObject item, JsonObject? view)
	{
		if (item.Count != 1) return null;
		var (key, value) = item.First();
		try
		{
			switch (key)
			{
				case "text" when value is JsonValue v && v.TryGetValue<string>(out var text):
					return new TextBlock(text);
				case "entity" when value is JsonValue v && v.TryGetValue<string>(out var reference):
					if (view is not null && Str(view, "title") is { Length: > 0 } title)
						return new EntityBlock(reference, Str(view, "kind"), Str(view, "code"), title, Str(view, "subtitle"), Str(view, "rgba"));
					var parts = reference.Split(':', 2);
					var label = view is not null && Str(view, "label") is { Length: > 0 } l ? l : parts.Length == 2 ? $"{parts[0].Replace('_', ' ')} {parts[1]}" : reference;
					return new EntityBlock(reference, parts[0], null, label, null, null);
				case "image" when value is JsonObject image && Str(image, "attachment_id") is { Length: > 0 } attachment:
					return new ImageBlock(Str(image, "entity") ?? "", attachment);
				case "table" when value is JsonObject table && table["columns"] is JsonArray columns && table["rows"] is JsonArray rows:
					var heads = columns.Select(c => c is JsonValue cv && cv.TryGetValue<string>(out var s) ? s : null).ToList();
					if (heads.Contains(null)) return null;
					var cells = new List<string[]>();
					foreach (var row in rows)
					{
						if (row is not JsonArray r || r.Count != heads.Count || r.Any(c => c is JsonObject or JsonArray)) return null;
						cells.Add(r.Select(c => c is null ? "—" : ChoiceText(c)).ToArray());
					}
					return new TableBlock(heads!.ToArray<string>(), cells);
				case "checklist" when value is JsonArray list && list.All(e => e is JsonValue ev && ev.TryGetValue<string>(out _)):
					return new ChecklistBlock(list.Select(e => e!.GetValue<string>()).ToArray());
				case "expect" when value is JsonObject expect && Num(expect, "weight_g") is { } weight:
					var tolerance = Num(expect, "tolerance_g");
					return new ExpectBlock($"Expect {Show(weight)} g{(tolerance is { } t ? $" ± {Show(t)} g" : "")}");
			}
		}
		catch (InvalidOperationException) { }
		return null;
	}

	static PairsBlock Pairs(JsonObject item) =>
		new(item.Select(p => (p.Key, PlainText(p.Value))).ToList());

	/// <summary>A value as text: a list of plain values as they read, anything deeper as JSON.</summary>
	static string PlainText(JsonNode? value)
	{
		if (value is null) return "—";
		if (value is JsonValue v && v.TryGetValue<string>(out var s)) return s;
		if (value is JsonArray list && list.Count > 0 && list.All(e => e is JsonValue)) return string.Join(", ", list.Select(ChoiceText));
		return value.ToJsonString();
	}

	/// <summary>A notification's text: the first entity card's title, or none (spec §6.5).</summary>
	public static string? NotificationText(DeviceTask task) =>
		Blocks(task).OfType<EntityBlock>().FirstOrDefault(b => b.Code is not null)?.Title;

	/// <summary>The problems of a 422 <c>invalid-answer</c>, each with its field ("" for the whole answer).</summary>
	public static List<(string Field, string Message)> ProblemsOf(string body)
	{
		try
		{
			if (JsonNode.Parse(body) is not JsonObject o || o["problems"] is not JsonArray problems) return [];
			return problems.OfType<JsonObject>()
				.Select(p => (Str(p, "field") ?? "", Str(p, "message") ?? ""))
				.Where(p => p.Item2.Length > 0).ToList();
		}
		catch (Exception error) when (error is JsonException or InvalidOperationException) { return []; }
	}

	/// <summary>Whether a 403 says this phone may not answer tasks (the setting, or the device's switch).</summary>
	public static bool TasksOff(string body)
	{
		try { return JsonNode.Parse(body) is JsonObject o && Str(o, "reason") == "device_tasks_off"; }
		catch (Exception error) when (error is JsonException or InvalidOperationException) { return false; }
	}

	/// <summary><c>task-scan</c>'s answer: the code read, what it names, and whether it fits the field.</summary>
	public static ScanResult? ScanOf(string body)
	{
		try
		{
			if (JsonNode.Parse(body) is not JsonObject o) return null;
			var resolved = o["resolved"] as JsonObject;
			return new ScanResult(
				Str(o, "code"),
				resolved is null ? null : Str(resolved, "name"),
				resolved is null ? null : Str(resolved, "kind"),
				o["fits"] is JsonValue f && f.TryGetValue<bool>(out var fits) && fits);
		}
		catch (Exception error) when (error is JsonException or InvalidOperationException) { return null; }
	}

	/// <summary><c>task-photo</c>'s answer: the attachment id a photo field answers.</summary>
	public static string? AttachmentOf(string body)
	{
		try { return JsonNode.Parse(body) is JsonObject o && Str(o, "attachment_id") is { Length: > 0 } id ? id : null; }
		catch (Exception error) when (error is JsonException or InvalidOperationException) { return null; }
	}

	/// <summary>The web task screen on the set origin. A fallback that is not a path of it opens the station page.</summary>
	public static string FallbackOf(string origin, string fallback)
	{
		var path = fallback.StartsWith('/') && !fallback.StartsWith("//") && !fallback.Contains('\\') ? fallback : "/station";
		return new Uri(new Uri(origin), path).ToString();
	}

	static readonly System.Text.RegularExpressions.Regex Code = new("^([A-Z])-?([0-9]{1,10})$", System.Text.RegularExpressions.RegexOptions.CultureInvariant);

	/// <summary>The kind each code prefix names (backend codes/resolve.py PREFIXES).</summary>
	static readonly Dictionary<char, string> Kinds = new() { ['L'] = "location", ['S'] = "spool", ['T'] = "spool_type", ['I'] = "item", ['K'] = "item_kind" };

	/// <summary>A typed code as the server writes it (<c>s03</c> is <c>S-3</c>); other text upper case, as the web's <c>canonicalCode</c>.</summary>
	public static string CanonicalCode(string text)
	{
		var upper = text.Trim().ToUpperInvariant();
		var found = Code.Match(upper);
		return found.Success && long.TryParse(found.Groups[2].Value, CultureInfo.InvariantCulture, out var n) ? $"{found.Groups[1].Value}-{n}" : upper;
	}

	/// <summary>Whether a typed code fits a field's <c>expects</c> (the web's <c>misfit</c> rule): one of its codes, or of its kinds, or it names neither.</summary>
	public static bool CodeFits(string text, JsonObject? expects)
	{
		var codes = (expects?["codes"] as JsonArray ?? []).Select(c => CanonicalCode(ChoiceText(c))).ToList();
		var kinds = (expects?["kinds"] as JsonArray ?? []).Select(ChoiceText).ToList();
		if (codes.Count == 0 && kinds.Count == 0) return true;
		var code = CanonicalCode(text);
		if (codes.Contains(code)) return true;
		return code.Length > 0 && Kinds.TryGetValue(code[0], out var kind) && Code.IsMatch(code) && kinds.Contains(kind);
	}

	/// <summary>The send button's text: a form of one confirm is that one large button (spec §6.4).</summary>
	public static string SendText(IReadOnlyList<FieldSpec> fields) =>
		fields is [{ Kind: ControlKind.Confirm } only] ? only.Label : "Send";

	static string? Str(JsonObject o, string key) => o[key] is JsonValue v && v.TryGetValue<string>(out var s) ? s : null;

	static double? Num(JsonObject o, string key) =>
		o[key] is JsonValue v && v.TryGetValue<double>(out var d) ? d : null;
}

/// <summary>A task as core's device routes send it (JSON names as #8181's <c>TaskQuestion</c>, plus <c>native</c>, <c>fallback_url</c>, <c>context_view</c>).</summary>
public sealed record DeviceTask(string Id, string RunId, string Title, JsonObject Schema, JsonArray Context, string? Place, string? Ui, string State, bool Native, string FallbackUrl, JsonArray? ContextView);

/// <summary>The native control a field takes (spec §6.4).</summary>
public enum ControlKind { Text, Email, Date, DateTime, Choice, Switch, Number, Lines, Confirm, Scan, Photo, Entity, Weight }

/// <summary>One field of a task's form.</summary>
public sealed record FieldSpec(string Name, string Label, ControlKind Kind, bool Required, JsonNode? Default, JsonArray? Enum, double? Min, double? Max, JsonObject? Expects, string? EntityKind)
{
	/// <summary>The field's JSON type.</summary>
	public string Type { get; init; } = "";
	/// <summary>A <see cref="ControlKind.Lines"/> field's item type.</summary>
	public string? ItemType { get; init; }
	public string? Description { get; init; }
	/// <summary>A choice's options as its buttons show them.</summary>
	public string[]? Options => Enum?.Select(TaskForm.ChoiceText).ToArray();
}

/// <summary>What a scan or a code photo named.</summary>
public sealed record ScanResult(string? Code, string? Name, string? Kind, bool Fits);

/// <summary>A context block as the screen draws it.</summary>
public abstract record ContextBlock;
public sealed record TextBlock(string Text) : ContextBlock;
public sealed record EntityBlock(string Ref, string? Kind, string? Code, string Title, string? Subtitle, string? Rgba) : ContextBlock;
public sealed record ImageBlock(string Entity, string AttachmentId) : ContextBlock;
public sealed record TableBlock(string[] Columns, List<string[]> Rows) : ContextBlock;
public sealed record ChecklistBlock(string[] Items) : ContextBlock;
public sealed record ExpectBlock(string Text) : ContextBlock;
public sealed record PairsBlock(List<(string Name, string Value)> Pairs) : ContextBlock;
