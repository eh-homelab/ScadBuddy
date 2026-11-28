namespace ScadBuddy;

/// <summary>
/// The open flow questions this phone follows (#8574, G6), oldest first, live from
/// <see cref="TaskInbox"/>. A tap opens the question's screen, or the web one.
/// </summary>
sealed class TaskListPage : ContentPage
{
	readonly VerticalStackLayout rows = new() { Spacing = 10 };
	readonly Action<DeviceTask> openWeb;

	public TaskListPage(Action<DeviceTask> openWeb)
	{
		this.openWeb = openWeb;
		Title = "Tasks";
		TaskTheme.Page(this);
		Content = new RefreshView
		{
			Content = new ScrollView { Content = new VerticalStackLayout { Padding = new Thickness(16, 20), Spacing = 16, Children = { rows } } },
			Command = new Command<RefreshView>(async _ => await Refresh()),
		}.Also(r => r.CommandParameter = r);
	}

	async Task Refresh()
	{
		await TaskInbox.Instance.Reload();
		if (Content is RefreshView view) view.IsRefreshing = false;
	}

	protected override void OnAppearing()
	{
		base.OnAppearing();
		TaskInbox.Instance.Changed += OnChanged;
		Draw();
		_ = TaskInbox.Instance.Reload();
	}

	protected override void OnDisappearing()
	{
		TaskInbox.Instance.Changed -= OnChanged;
		base.OnDisappearing();
	}

	void OnChanged() => MainThread.BeginInvokeOnMainThread(Draw);

	void Draw()
	{
		rows.Clear();
		var open = TaskInbox.Instance.Open;
		if (open.Count == 0)
		{
			rows.Add(TaskTheme.Body(TaskInbox.Instance.Off ? "This phone does not get flow questions." : "No questions for you right now.", 18));
			rows.Add(TaskTheme.Small(TaskInbox.Instance.Off
				? "Turn on Flow questions for this phone on ScadBuddy's Devices page."
				: "When a flow asks you something, it shows here and as a notification. Pull down to look again."));
			return;
		}
		foreach (var task in open)
		{
			var text = new VerticalStackLayout { Spacing = 4 };
			text.Add(TaskTheme.Body(task.Title, 17).Also(l => l.FontFamily = "OpenSansSemibold"));
			var where = task.Place is { Length: > 0 } place ? "At " + place : null;
			var what = TaskForm.NotificationText(task);
			var line = string.Join("  ", new[] { what, where }.Where(s => s is not null));
			if (line.Length > 0) text.Add(TaskTheme.Small(line));
			if (!task.Native) text.Add(TaskTheme.Small("Opens in the web view"));
			var card = TaskTheme.Card(text);
			card.MinimumHeightRequest = TaskTheme.Touch + 12;
			card.GestureRecognizers.Add(new TapGestureRecognizer { Command = new Command(async () => await TaskPage.Open(Navigation, task, openWeb, this)) });
			SemanticProperties.SetDescription(card, task.Title);
			rows.Add(card);
		}
	}
}
