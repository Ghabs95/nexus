"""Tests for user_manager module."""
import json
import pytest
from pathlib import Path
from datetime import datetime
from user_manager import UserManager, User, UserProject


class TestUserManager:
    """Tests for UserManager class."""
    
    def test_initialization(self, tmp_path):
        """Test UserManager initialization."""
        data_file = tmp_path / "users.json"
        manager = UserManager(data_file)
        
        assert manager.data_file == data_file
        assert manager.users == {}
    
    def test_create_new_user(self, tmp_path):
        """Test creating a new user."""
        data_file = tmp_path / "users.json"
        manager = UserManager(data_file)
        
        user = manager.get_or_create_user(
            telegram_id=12345,
            username="testuser",
            first_name="Test"
        )
        
        assert user.telegram_id == 12345
        assert user.username == "testuser"
        assert user.first_name == "Test"
        assert user.projects == {}
        assert 12345 in manager.users
    
    def test_get_existing_user(self, tmp_path):
        """Test getting an existing user updates last_seen."""
        data_file = tmp_path / "users.json"
        manager = UserManager(data_file)
        
        # Create user
        user1 = manager.get_or_create_user(12345, "user1", "User")
        first_seen = user1.last_seen
        
        # Get same user
        user2 = manager.get_or_create_user(12345, "user1_updated")
        
        assert user1 is user2  # Same object
        assert user2.username == "user1_updated"
        assert user2.last_seen > first_seen  # Updated timestamp
    
    def test_track_issue(self, tmp_path):
        """Test tracking an issue for a user."""
        data_file = tmp_path / "users.json"
        manager = UserManager(data_file)
        
        manager.track_issue(
            telegram_id=12345,
            project="casit",
            issue_number="123",
            username="testuser"
        )
        
        user = manager.users[12345]
        assert "casit" in user.projects
        assert "123" in user.projects["casit"].tracked_issues
    
    def test_track_multiple_issues_per_project(self, tmp_path):
        """Test tracking multiple issues in same project."""
        data_file = tmp_path / "users.json"
        manager = UserManager(data_file)
        
        manager.track_issue(12345, "casit", "123")
        manager.track_issue(12345, "casit", "456")
        manager.track_issue(12345, "casit", "789")
        
        user = manager.users[12345]
        assert len(user.projects["casit"].tracked_issues) == 3
        assert "123" in user.projects["casit"].tracked_issues
        assert "456" in user.projects["casit"].tracked_issues
        assert "789" in user.projects["casit"].tracked_issues
    
    def test_track_issues_across_multiple_projects(self, tmp_path):
        """Test tracking issues across different projects."""
        data_file = tmp_path / "users.json"
        manager = UserManager(data_file)
        
        manager.track_issue(12345, "casit", "123")
        manager.track_issue(12345, "wlbl", "456")
        manager.track_issue(12345, "bm", "789")
        
        user = manager.users[12345]
        assert len(user.projects) == 3
        assert "casit" in user.projects
        assert "wlbl" in user.projects
        assert "bm" in user.projects
    
    def test_track_duplicate_issue_ignored(self, tmp_path):
        """Test tracking same issue twice doesn't create duplicates."""
        data_file = tmp_path / "users.json"
        manager = UserManager(data_file)
        
        manager.track_issue(12345, "casit", "123")
        manager.track_issue(12345, "casit", "123")
        manager.track_issue(12345, "casit", "123")
        
        user = manager.users[12345]
        assert len(user.projects["casit"].tracked_issues) == 1
    
    def test_untrack_issue(self, tmp_path):
        """Test untracking an issue."""
        data_file = tmp_path / "users.json"
        manager = UserManager(data_file)
        
        manager.track_issue(12345, "casit", "123")
        assert "123" in manager.users[12345].projects["casit"].tracked_issues
        
        result = manager.untrack_issue(12345, "casit", "123")
        assert result is True
        assert "123" not in manager.users[12345].projects["casit"].tracked_issues
    
    def test_untrack_nonexistent_issue(self, tmp_path):
        """Test untracking an issue that wasn't tracked."""
        data_file = tmp_path / "users.json"
        manager = UserManager(data_file)
        
        result = manager.untrack_issue(12345, "casit", "999")
        assert result is False
    
    def test_get_user_tracked_issues_all_projects(self, tmp_path):
        """Test getting all tracked issues for a user."""
        data_file = tmp_path / "users.json"
        manager = UserManager(data_file)
        
        manager.track_issue(12345, "casit", "123")
        manager.track_issue(12345, "casit", "456")
        manager.track_issue(12345, "wlbl", "789")
        
        tracked = manager.get_user_tracked_issues(12345)
        
        assert len(tracked) == 2
        assert "casit" in tracked
        assert "wlbl" in tracked
        assert tracked["casit"] == ["123", "456"]
        assert tracked["wlbl"] == ["789"]
    
    def test_get_user_tracked_issues_specific_project(self, tmp_path):
        """Test getting tracked issues for specific project."""
        data_file = tmp_path / "users.json"
        manager = UserManager(data_file)
        
        manager.track_issue(12345, "casit", "123")
        manager.track_issue(12345, "wlbl", "789")
        
        tracked = manager.get_user_tracked_issues(12345, project="casit")
        
        assert len(tracked) == 1
        assert "casit" in tracked
        assert "wlbl" not in tracked
    
    def test_get_issue_trackers(self, tmp_path):
        """Test getting all users tracking an issue."""
        data_file = tmp_path / "users.json"
        manager = UserManager(data_file)
        
        manager.track_issue(111, "casit", "123")
        manager.track_issue(222, "casit", "123")
        manager.track_issue(333, "casit", "456")
        
        trackers = manager.get_issue_trackers("casit", "123")
        
        assert len(trackers) == 2
        assert 111 in trackers
        assert 222 in trackers
        assert 333 not in trackers
    
    def test_get_user_stats(self, tmp_path):
        """Test getting user statistics."""
        data_file = tmp_path / "users.json"
        manager = UserManager(data_file)
        
        manager.track_issue(12345, "casit", "123", username="testuser", first_name="Test")
        manager.track_issue(12345, "casit", "456")
        manager.track_issue(12345, "wlbl", "789")
        
        stats = manager.get_user_stats(12345)
        
        assert stats['exists'] is True
        assert stats['username'] == "testuser"
        assert stats['first_name'] == "Test"
        assert len(stats['projects']) == 2
        assert stats['total_tracked_issues'] == 3
    
    def test_get_nonexistent_user_stats(self, tmp_path):
        """Test getting stats for user that doesn't exist."""
        data_file = tmp_path / "users.json"
        manager = UserManager(data_file)
        
        stats = manager.get_user_stats(99999)
        assert stats['exists'] is False
    
    def test_get_all_users_stats(self, tmp_path):
        """Test getting overall statistics."""
        data_file = tmp_path / "users.json"
        manager = UserManager(data_file)
        
        manager.track_issue(111, "casit", "123")
        manager.track_issue(222, "wlbl", "456")
        manager.track_issue(333, "bm", "789")
        manager.track_issue(333, "casit", "999")
        
        stats = manager.get_all_users_stats()
        
        assert stats['total_users'] == 3
        assert stats['total_projects'] == 3
        assert set(stats['projects']) == {'casit', 'wlbl', 'bm'}
        assert stats['total_tracked_issues'] == 4
    
    def test_save_and_load_users(self, tmp_path):
        """Test persisting and loading user data."""
        data_file = tmp_path / "users.json"
        
        # Create and populate manager
        manager1 = UserManager(data_file)
        manager1.track_issue(12345, "casit", "123", username="user1")
        manager1.track_issue(67890, "wlbl", "456", username="user2")
        
        # Create new manager from same file
        manager2 = UserManager(data_file)
        
        assert len(manager2.users) == 2
        assert 12345 in manager2.users
        assert 67890 in manager2.users
        assert manager2.users[12345].username == "user1"
        assert "123" in manager2.users[12345].projects["casit"].tracked_issues
    
    def test_multi_user_isolation(self, tmp_path):
        """Test that different users' tracking is isolated."""
        data_file = tmp_path / "users.json"
        manager = UserManager(data_file)
        
        manager.track_issue(111, "casit", "123")
        manager.track_issue(222, "casit", "456")
        
        user1_issues = manager.get_user_tracked_issues(111)
        user2_issues = manager.get_user_tracked_issues(222)
        
        assert user1_issues["casit"] == ["123"]
        assert user2_issues["casit"] == ["456"]
