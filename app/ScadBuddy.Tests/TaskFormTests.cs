using System.Text.Json.Nodes;
using ScadBuddy;
using Xunit;

namespace ScadBuddy.Tests;

public class TaskFormTests
{
	static DeviceTask Fixture(string name)
	{
		var text = File.ReadAllText(Path.Combine(AppContext.BaseDirectory, "fixtures", "tasks", name + ".json"));
		return TaskForm.TaskOf(JsonNode.Parse(text)!.AsObject()) ?? throw new InvalidOperationException(name + " is no task");
	}

	static JsonObject Schema(string properties, string required = "[]") =>
		JsonNode.Parse($$"""{"type":"object","properties":{{properties}},"required":{{required}}}""")!.AsObject();

	[Theory]
	[InlineData("field-text", ControlKind.Text)]
	[InlineData("field-email", ControlKind.Email)]
	[InlineData("field-date", ControlKind.Date)]
	[InlineData("field-date-time", ControlKind.DateTime)]
	[InlineData("field-choice", ControlKind.Choice)]
	[InlineData("field-switch", ControlKind.Switch)]
	[InlineData("field-number", ControlKind.Number)]
	[InlineData("field-lines", ControlKind.Lines)]
	[InlineData("field-confirm", ControlKind.Confirm)]
	[InlineData("field-scan", ControlKind.Scan)]
	[InlineData("field-photo", ControlKind.Photo)]
	[InlineData("field-entity-spool", ControlKind.Entity)]
	[InlineData("field-entity-location", ControlKind.Entity)]
	[InlineData("field-weight", ControlKind.Weight)]
	public void EachFixtureMapsToItsControl(string fixture, ControlKind kind)
	{
		var task = Fixture(fixture);
		var fields = TaskForm.Fields(task.Schema);
		Assert.NotNull(fields);
		var field = Assert.Single(fields);
		Assert.Equal("f", field.Name);
		Assert.Equal(kind, field.Kind);
		Assert.True(field.Required);
		Assert.True(task.Native);
	}

	[Theory]
	[InlineData("field-file")]
	[InlineData("field-entity-library-file")]
	[InlineData("field-unknown-widget")]
	public void AFieldTheAppCannotDrawIsNotNative(string fixture)
	{
		var task = Fixture(fixture);
		Assert.Null(TaskForm.Fields(task.Schema));
		Assert.False(task.Native);
	}

	[Fact]
	public void AnUnknownWidgetIsNotNative() =>
		Assert.Null(TaskForm.Fields(Schema("""{"c":{"type":"string","x-sb-widget":"colour-wheel"}}""")));

	[Fact]
	public void AStringOfAnotherFormatIsNotNative() =>
		Assert.Null(TaskForm.Fields(Schema("""{"u":{"type":"string","format":"uri"}}""")));

	[Fact]
	public void AnArrayOfObjectsIsNotNative() =>
		Assert.Null(TaskForm.Fields(Schema("""{"a":{"type":"array","items":{"type":"object"}}}""")));

	[Fact]
	public void ANestedObjectIsNotNative() =>
		Assert.Null(TaskForm.Fields(Schema("""{"o":{"type":"object","properties":{}}}""")));

	[Fact]
	public void ASchemaWithNoFieldsHasNone() => Assert.Empty(TaskForm.Fields(Schema("{}"))!);

	[Fact]
	public void TheEntityKindAndTheExpectsAreKept()
	{
		var spool = Assert.Single(Fixture("field-entity-spool").Schema is var s ? TaskForm.Fields(s)! : []);
		Assert.Equal("spool", spool.EntityKind);
		var scan = Assert.Single(TaskForm.Fields(Fixture("field-scan").Schema)!);
		Assert.Equal("S-9", scan.Expects!["codes"]![0]!.GetValue<string>());
	}

	[Fact]
	public void TheBoundsAndTheOptionsAreKept()
	{
		var number = Assert.Single(TaskForm.Fields(Fixture("field-number").Schema)!);
		Assert.Equal(1, number.Min);
		Assert.Equal(10, number.Max);
		var choice = Assert.Single(TaskForm.Fields(Fixture("field-choice").Schema)!);
		Assert.Equal(["English", "French"], choice.Options!);
		Assert.Equal("English", choice.Default!.GetValue<string>());
	}

	[Fact]
	public void LabelsComeFromTitleElseTheName()
	{
		var fields = TaskForm.Fields(Schema("""{"pickup_at":{"type":"string"},"lang":{"type":"string","title":"Language"}}"""))!;
		Assert.Equal("pickup at", fields[0].Label);
		Assert.Equal("Language", fields[1].Label);
	}

	[Fact]
	public void AConfirmAnswersTrue()
	{
		var fields = TaskForm.Fields(Fixture("field-confirm").Schema)!;
		var answer = TaskForm.Answer(fields, new Dictionary<string, JsonNode?>());
		Assert.True(answer["f"]!.GetValue<bool>());
	}

	[Fact]
	public void TheAnswerLeavesOutAnEmptyOptionalField()
	{
		var fields = TaskForm.Fields(Schema("""{"note":{"type":"string"},"n":{"type":"number"},"tags":{"type":"array","items":{"type":"string"}}}"""))!;
		var answer = TaskForm.Answer(fields, new Dictionary<string, JsonNode?> { ["note"] = "  ", ["n"] = "", ["tags"] = "\n \n" });
		Assert.Empty(answer);
	}

	[Fact]
	public void NumbersAndWeightsAnswerAsNumbers()
	{
		var fields = TaskForm.Fields(Schema("""{"n":{"type":"integer"},"w":{"type":"number","x-sb-widget":"weight"}}"""))!;
		var answer = TaskForm.Answer(fields, new Dictionary<string, JsonNode?> { ["n"] = "3", ["w"] = "812.5" });
		Assert.Equal("""{"n":3,"w":812.5}""", answer.ToJsonString());
	}

	[Fact]
	public void LinesAnswerAsAnArrayOfTheItemType()
	{
		var fields = TaskForm.Fields(Schema("""{"names":{"type":"array","items":{"type":"string"}},"ns":{"type":"array","items":{"type":"integer"}}}"""))!;
		var answer = TaskForm.Answer(fields, new Dictionary<string, JsonNode?> { ["names"] = "Jo\n\n Sam \n", ["ns"] = "1\n2" });
		Assert.Equal("""{"names":["Jo","Sam"],"ns":[1,2]}""", answer.ToJsonString());
	}

	[Fact]
	public void AChoiceAnswersTheEnumValueItself()
	{
		var fields = TaskForm.Fields(Schema("""{"copies":{"type":"integer","enum":[1,2,5]}}"""))!;
		Assert.Equal(ControlKind.Choice, fields[0].Kind);
		Assert.Equal(["1", "2", "5"], fields[0].Options!);
		var answer = TaskForm.Answer(fields, new Dictionary<string, JsonNode?> { ["copies"] = "2" });
		Assert.Equal("""{"copies":2}""", answer.ToJsonString());
	}

	[Fact]
	public void ASwitchAnswersItsState()
	{
		var fields = TaskForm.Fields(Fixture("field-switch").Schema)!;
		Assert.Equal("""{"f":false}""", TaskForm.Answer(fields, new Dictionary<string, JsonNode?> { ["f"] = false }).ToJsonString());
	}

	[Fact]
	public void TheCheckNamesAMissingRequiredFieldAndABadNumber()
	{
		var fields = TaskForm.Fields(Schema("""{"spool":{"type":"string","x-sb-widget":"scan","title":"Spool"},"n":{"type":"integer","minimum":1}}""", """["spool"]"""))!;
		var problems = TaskForm.Check(fields, new Dictionary<string, JsonNode?> { ["n"] = "1.5" });
		Assert.Contains(("spool", "Spool is needed."), problems);
		Assert.Contains(("n", "Give a whole number."), problems);
		Assert.Contains(("n", "Give at least 1."), TaskForm.Check(fields, new Dictionary<string, JsonNode?> { ["spool"] = "S-9", ["n"] = "0" }));
		Assert.Empty(TaskForm.Check(fields, new Dictionary<string, JsonNode?> { ["spool"] = "S-9", ["n"] = "4" }));
	}

	[Fact]
	public void ARequiredSwitchIsNotMissingWhenOff()
	{
		var fields = TaskForm.Fields(Fixture("field-switch").Schema)!;
		Assert.Empty(TaskForm.Check(fields, new Dictionary<string, JsonNode?> { ["f"] = false }));
	}

	[Fact]
	public void ADateTimeIsSentWithItsOffset() =>
		Assert.Equal("2026-11-28T17:30:00-05:00", TaskForm.DateTimeText(new DateTime(2026, 11, 28, 17, 30, 0), TimeSpan.FromHours(-5)));

	[Fact]
	public void ADateIsSentAsADay() => Assert.Equal("2026-11-28", TaskForm.DateText(new DateTime(2026, 11, 28, 17, 30, 0)));

	[Theory]
	[InlineData("block-entity", typeof(EntityBlock))]
	[InlineData("block-text", typeof(TextBlock))]
	[InlineData("block-image", typeof(ImageBlock))]
	[InlineData("block-table", typeof(TableBlock))]
	[InlineData("block-checklist", typeof(ChecklistBlock))]
	[InlineData("block-expect", typeof(ExpectBlock))]
	[InlineData("block-pairs", typeof(PairsBlock))]
	public void EachContextFixtureIsItsBlock(string fixture, Type kind) =>
		Assert.IsType(kind, Assert.Single(TaskForm.Blocks(Fixture(fixture))));

	[Fact]
	public void AnEntityBlockTakesItsCardFromTheContextView()
	{
		var card = Assert.IsType<EntityBlock>(Assert.Single(TaskForm.Blocks(Fixture("block-entity"))));
		Assert.Equal("S-36", card.Code);
		Assert.Equal("Bambu PLA Basic Gold", card.Title);
		Assert.Equal("812 g left", card.Subtitle);
		Assert.Equal("#E4BD68FF", card.Rgba);
	}

	[Fact]
	public void AnEntityBlockWithNoCardShowsItsRef()
	{
		var task = Fixture("block-entity") with { ContextView = null };
		var card = Assert.IsType<EntityBlock>(Assert.Single(TaskForm.Blocks(task)));
		Assert.Equal("spool 0f6a1c2e-3b4d-4e5f-8a9b-0c1d2e3f4a5b", card.Title);
		Assert.Null(card.Code);
	}

	[Fact]
	public void AnExpectReadsAsTheWebSaysIt() =>
		Assert.Equal("Expect 640 g ± 20 g", Assert.IsType<ExpectBlock>(Assert.Single(TaskForm.Blocks(Fixture("block-expect")))).Text);

	[Fact]
	public void ATableKeepsItsCellsAsText()
	{
		var table = Assert.IsType<TableBlock>(Assert.Single(TaskForm.Blocks(Fixture("block-table"))));
		Assert.Equal(["Plate", "Grams"], table.Columns);
		Assert.Equal(["2", "—"], table.Rows[1]);
		Assert.Equal("12.5", table.Rows[0][1]);
	}

	[Fact]
	public void PairsListAPlainListAsText()
	{
		var pairs = Assert.IsType<PairsBlock>(Assert.Single(TaskForm.Blocks(Fixture("block-pairs"))));
		Assert.Equal([("order", "SB-1042"), ("items", "name keychain, cable clip")], pairs.Pairs);
	}

	[Fact]
	public void TheNotificationTextIsTheFirstCardsTitle()
	{
		Assert.Equal("Bambu PLA Basic Gold", TaskForm.NotificationText(Fixture("block-entity")));
		Assert.Null(TaskForm.NotificationText(Fixture("block-text")));
	}

	[Fact]
	public void AListGivesItsTasks()
	{
		var one = File.ReadAllText(Path.Combine(AppContext.BaseDirectory, "fixtures", "tasks", "field-scan.json"));
		var tasks = TaskForm.ListOf($$"""{"tasks":[{{one}},{"id":"broken"}]}""");
		var task = Assert.Single(tasks!);
		Assert.Equal("Fixture scan", task.Title);
		Assert.Null(TaskForm.ListOf("not json"));
	}

	[Fact]
	public void ATaskOfAnotherShapeIsNone()
	{
		Assert.Null(TaskForm.TaskOf(JsonNode.Parse("""{"id":"x","title":"t"}""")!.AsObject()));
		Assert.Null(TaskForm.TaskOf(JsonNode.Parse("""{"id":"x","run_id":"r","title":"t","schema":[],"state":"open"}""")!.AsObject()));
	}

	[Fact]
	public void AProblemListNamesEachField()
	{
		var problems = TaskForm.ProblemsOf("""{"type":"https://scadbuddy.dev/problems/tasks/invalid-answer","problems":[{"field":"email","message":"not a valid email"},{"field":"","message":"spool is required"}]}""");
		Assert.Equal([("email", "not a valid email"), ("", "spool is required")], problems);
		Assert.Empty(TaskForm.ProblemsOf("<html>"));
	}

	[Fact]
	public void TheTasksAreOffOnlyForItsReason()
	{
		Assert.True(TaskForm.TasksOff("""{"type":"x","reason":"device_tasks_off"}"""));
		Assert.False(TaskForm.TasksOff("""{"reason":"device_unauthorized"}"""));
		Assert.False(TaskForm.TasksOff("nope"));
	}

	[Fact]
	public void AScanAnswerGivesItsCodeAndWhetherItFits()
	{
		var scan = TaskForm.ScanOf("""{"code":"S-36","resolved":{"kind":"spool","id":"u","code":"S-36","name":"Bambu PLA Basic Gold","archived":false},"fits":true}""");
		Assert.Equal("S-36", scan!.Code);
		Assert.Equal("Bambu PLA Basic Gold", scan.Name);
		Assert.True(scan.Fits);
		var none = TaskForm.ScanOf("""{"code":null,"resolved":null,"fits":false}""");
		Assert.Null(none!.Code);
		Assert.False(none.Fits);
	}

	[Fact]
	public void APhotoAnswerGivesItsAttachment()
	{
		Assert.Equal("a1", TaskForm.AttachmentOf("""{"attachment_id":"a1"}"""));
		Assert.Null(TaskForm.AttachmentOf("{}"));
	}

	[Fact]
	public void TheWebScreenIsOnTheSetOrigin()
	{
		Assert.Equal("https://sb.example/station/L-7", TaskForm.FallbackOf("https://sb.example", "/station/L-7"));
		// A fallback that names another origin stays on the set one.
		Assert.Equal("https://sb.example/station", TaskForm.FallbackOf("https://sb.example", "https://evil.example/x"));
		Assert.Equal("https://sb.example/station", TaskForm.FallbackOf("https://sb.example", "//evil.example/x"));
	}

	[Theory]
	[InlineData("s03", "S-3")]
	[InlineData(" l-12 ", "L-12")]
	[InlineData("T7", "T-7")]
	[InlineData("hello", "HELLO")]
	public void ATypedCodeIsWrittenAsTheServerWritesIt(string typed, string code) =>
		Assert.Equal(code, TaskForm.CanonicalCode(typed));

	[Fact]
	public void ATypedCodeFitsWhatTheFieldExpects()
	{
		var expects = JsonNode.Parse("""{"codes":["S-9"],"kinds":["location"]}""")!.AsObject();
		Assert.True(TaskForm.CodeFits("s9", expects));
		Assert.True(TaskForm.CodeFits("L-4", expects));
		Assert.False(TaskForm.CodeFits("S-10", expects));
		Assert.True(TaskForm.CodeFits("S-10", null));
	}

	[Fact]
	public void ASingleConfirmIsTheSendButton()
	{
		Assert.Equal("The plate is clear", TaskForm.SendText(TaskForm.Fields(Fixture("field-confirm").Schema)!));
		Assert.Equal("Send", TaskForm.SendText(TaskForm.Fields(Fixture("field-scan").Schema)!));
	}
}
