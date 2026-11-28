using System.Text.Json.Nodes;

namespace ScadBuddy;

/// <summary>
/// One flow question, drawn natively from its schema and context (#8574, spec §6.3, §6.4): the
/// title, the context, a control per field, and one send button pinned at the bottom. A task the
/// app cannot draw opens the web task screen instead (<see cref="Open"/>). The answer goes
/// through core's device route to the same check as a web answer.
/// </summary>
sealed class TaskPage : ContentPage
{
	readonly DeviceTask task;
	readonly IReadOnlyList<FieldSpec> fields;
	readonly List<TaskControl> controls = [];
	readonly Label formProblem = TaskTheme.Problem();
	readonly Button send;
	// One answer keeps its key on every try, so a reply lost on the way is not answered twice.
	string key = Guid.NewGuid().ToString();
	bool sending, closed;

	TaskPage(DeviceTask task, IReadOnlyList<FieldSpec> fields)
	{
		this.task = task;
		this.fields = fields;
		Title = "Task";
		TaskTheme.Page(this);

		var stack = new VerticalStackLayout { Padding = new Thickness(16, 20, 16, 24), Spacing = 18 };
		stack.Add(TaskTheme.Title(task.Title));
		if (task.Place is { Length: > 0 } place) stack.Add(TaskTheme.Small("At " + place));
		foreach (var block in TaskForm.Blocks(task)) stack.Add(ContextViews.For(block, task.Id));
		foreach (var field in fields)
		{
			var control = TaskControl.For(field, task.Id, this);
			controls.Add(control);
			stack.Add(control);
		}
		stack.Add(formProblem);

		send = TaskTheme.Primary(TaskForm.SendText(fields));
		send.Clicked += async (_, _) => await Send();
		var grid = new Grid { RowDefinitions = [new(GridLength.Star), new(GridLength.Auto)] };
		grid.Add(new ScrollView { Content = stack }, 0, 0);
		grid.Add(new ContentView { Padding = new Thickness(16, 8, 16, 16), Content = send }, 0, 1);
		Content = grid;
	}

	/// <summary>
	/// Opens a task: its native screen, or the web task screen when the app cannot draw it
	/// (<c>native</c> false, a <c>ui</c> module, or a field the app does not know: never a half form).
	/// </summary>
	public static async Task Open(INavigation navigation, DeviceTask listed, Action<DeviceTask> openWeb, Page host)
	{
		DeviceTask? task;
		try { task = await TaskInbox.Instance.Get(listed.Id); }
		catch (Exception)
		{
			await host.DisplayAlertAsync("Not opened", "ScadBuddy could not be reached. Check the connection and open the question again.", "OK");
			return;
		}
		if (task is null || task.State != "open")
		{
			await host.DisplayAlertAsync("Answered", "This question is no longer open.", "OK");
			return;
		}
		if (!task.Native || task.Ui is not null || TaskForm.Fields(task.Schema) is not { } fields)
		{
			openWeb(task);
			return;
		}
		await navigation.PushAsync(new TaskPage(task, fields));
	}

	protected override void OnAppearing()
	{
		base.OnAppearing();
		TaskInbox.Instance.Left += OnLeft;
	}

	protected override void OnDisappearing()
	{
		TaskInbox.Instance.Left -= OnLeft;
		base.OnDisappearing();
	}

	// Answered on the web (or cancelled) while the phone shows it.
	void OnLeft(string id)
	{
		if (id != task.Id || sending) return;
		MainThread.BeginInvokeOnMainThread(async () => await Close("Someone already answered this question."));
	}

	async Task Close(string? why)
	{
		if (closed) return;
		closed = true;
		if (why is not null) await DisplayAlertAsync("Answered", why, "OK");
		if (Navigation.NavigationStack.Contains(this)) await Navigation.PopAsync();
	}

	async Task Send()
	{
		if (sending || closed) return;
		var values = controls.ToDictionary(c => c.Field.Name, c => c.Value);
		var problems = TaskForm.Check(fields, values);
		var shown = false;
		foreach (var control in controls)
		{
			var problem = control.LocalProblem ?? problems.FirstOrDefault(p => p.Field == control.Field.Name).Message;
			control.Show(problem);
			shown |= problem is not null;
		}
		formProblem.IsVisible = false;
		if (shown) return;

		sending = true;
		send.IsEnabled = false;
		var label = send.Text;
		send.Text = "Sending…";
		try
		{
			var result = await TaskInbox.Instance.Answer(task.Id, TaskForm.Answer(fields, values), key);
			switch (result.Status)
			{
				case 200 or 202:
					NfcBridge.Notify("Sent");
					await Close(null);
					return;
				case 409 or 404:
					await Close("Someone already answered this question.");
					return;
				case 422:
					// The answer was refused: a changed answer is a new one, with a key of its own.
					key = Guid.NewGuid().ToString();
					foreach (var control in controls)
						control.Show(result.Problems.FirstOrDefault(p => p.Field == control.Field.Name || p.Field.StartsWith(control.Field.Name + "/")).Message);
					var rest = result.Problems.Where(p => controls.All(c => p.Field != c.Field.Name && !p.Field.StartsWith(c.Field.Name + "/"))).Select(p => p.Message).ToList();
					Form(rest.Count > 0 ? string.Join("\n", rest) : result.Problems.Count == 0 ? "ScadBuddy refused the answer." : null);
					return;
				case 403 when TaskInbox.Instance.Off:
					await Close("This phone may not answer questions now. Turn on Flow questions for it on ScadBuddy's Devices page.");
					return;
				case 401:
					await Close("This phone's pairing with ScadBuddy ended.");
					return;
				case 0:
					Form("Not sent. Check the connection and send again.");
					return;
				default:
					Form($"Not sent: ScadBuddy answered {result.Status}. Send again.");
					return;
			}
		}
		finally
		{
			sending = false;
			send.IsEnabled = true;
			send.Text = label;
		}
	}

	void Form(string? text)
	{
		formProblem.Text = text;
		formProblem.IsVisible = text is not null;
	}
}
