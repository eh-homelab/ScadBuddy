using Android.App;
using Android.Content;
using Android.OS;

namespace ScadBuddy;

/// <summary>
/// The Android notifications of flow questions (#8574, spec §6.3, §6.5). Channel
/// <c>questions</c> ("Questions for you", high) has one notification per open task, tagged with
/// its id: title the task's title, text its first entity card's title, private on the lock
/// screen (a public version says only "ScadBuddy: Question for you"). A tap opens the task's
/// screen. Channel <c>listening</c> (min) holds the service's own notification. No answer
/// actions in this phase, and no task content beyond the title and that card.
/// </summary>
static class TaskNotifications
{
	public const string QuestionsChannel = "questions";
	public const string ListeningChannel = "listening";
	/// <summary>The extra a tap's intent carries: the task to open.</summary>
	public const string TaskExtra = "task_id";
	const int QuestionId = 1;

	static bool following;
	// The question waits for the app in front: Android asks only from an activity.
	static bool askWaiting;
	static bool asked;

	public static void Channels(Context context)
	{
		var manager = (NotificationManager)context.GetSystemService(Context.NotificationService)!;
		manager.CreateNotificationChannel(new NotificationChannel(QuestionsChannel, "Questions for you", NotificationImportance.High)
		{
			Description = "A flow asks you something: a tag to read, a photo, a choice.",
			LockscreenVisibility = NotificationVisibility.Private,
		});
		manager.CreateNotificationChannel(new NotificationChannel(ListeningChannel, "ScadBuddy is listening for questions", NotificationImportance.Min)
		{
			Description = "Shown while the app keeps its link to ScadBuddy open for flow questions.",
		});
	}

	/// <summary>The service's own notification.</summary>
	public static Notification Listening(Context context) =>
		new Notification.Builder(context, ListeningChannel)
			.SetSmallIcon(Android.Resource.Drawable.StatNotifySync)
			.SetContentTitle("ScadBuddy is listening for questions")
			.SetContentIntent(Open(context, null))
			.SetOngoing(true)
			.SetShowWhen(false)
			.Build()!;

	/// <summary>Posts and cancels the questions as the inbox changes. Once per process.</summary>
	public static void Follow(Context context)
	{
		if (following) return;
		following = true;
		var app = context.ApplicationContext!;
		TaskInbox.Instance.Arrived += task => Post(app, task);
		TaskInbox.Instance.Left += id => Cancel(app, id);
		// The open tasks already known (the list was read before the service ran).
		foreach (var task in TaskInbox.Instance.Open) Post(app, task);
	}

	static void Post(Context context, DeviceTask task)
	{
		if (Build.VERSION.SdkInt >= BuildVersionCodes.Tiramisu && context.CheckSelfPermission(Android.Manifest.Permission.PostNotifications) != Android.Content.PM.Permission.Granted)
		{
			// Android 13+ asks the first time a task arrives (spec §6.3).
			askWaiting = true;
			MainThread.BeginInvokeOnMainThread(() => _ = AskIfWaiting());
			return;
		}
		var publicVersion = new Notification.Builder(context, QuestionsChannel)
			.SetSmallIcon(Android.Resource.Drawable.StatNotifyChat)
			.SetContentTitle("ScadBuddy: Question for you")
			.Build();
		var builder = new Notification.Builder(context, QuestionsChannel)
			.SetSmallIcon(Android.Resource.Drawable.StatNotifyChat)
			.SetContentTitle(task.Title)
			.SetContentIntent(Open(context, task.Id))
			.SetAutoCancel(true)
			.SetCategory(Notification.CategoryReminder)
			.SetVisibility(NotificationVisibility.Private)
			.SetPublicVersion(publicVersion);
		if (TaskForm.NotificationText(task) is { } text) builder.SetContentText(text);
		var manager = (NotificationManager)context.GetSystemService(Context.NotificationService)!;
		manager.Notify(task.Id, QuestionId, builder.Build());
	}

	static void Cancel(Context context, string id)
	{
		var manager = (NotificationManager)context.GetSystemService(Context.NotificationService)!;
		manager.Cancel(id, QuestionId);
	}

	/// <summary>Asks for POST_NOTIFICATIONS once a task wanted it, when an activity is in front.</summary>
	public static async Task AskIfWaiting()
	{
		if (!askWaiting || asked || Platform.CurrentActivity is null) return;
		asked = true;
		try
		{
			if (await Permissions.RequestAsync<Permissions.PostNotifications>() != PermissionStatus.Granted) return;
		}
		catch (Exception) { asked = false; return; }
		askWaiting = false;
		var app = Android.App.Application.Context;
		foreach (var task in TaskInbox.Instance.Open) Post(app, task);
	}

	static PendingIntent Open(Context context, string? task)
	{
		var intent = new Intent(context, typeof(MainActivity));
		intent.SetFlags(ActivityFlags.SingleTop | ActivityFlags.ClearTop);
		if (task is not null)
		{
			intent.PutExtra(TaskExtra, task);
			// One intent per task, so each notification opens its own.
			intent.SetAction("scadbuddy.task." + task);
		}
		return PendingIntent.GetActivity(context, task?.GetHashCode() ?? 0, intent, PendingIntentFlags.UpdateCurrent | PendingIntentFlags.Immutable)!;
	}
}
